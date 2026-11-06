# DATA_MODEL.md — Database, Spreadsheet & Webhook Schema Specification

> **Single Source of Truth for Data Structures:** Every SQLAlchemy model (`src/models.py`), Pydantic schema (`src/schemas.py`), and Google Apps Script payload (`apps_script/Code.gs`) MUST match the schemas defined here.

## 1. Google Sheets Frontend Schema (`Invoices_Ledger` Tab)
The business owner works inside a standard Google Sheet with the following exact columns (Row 1 headers):

| Col | Header Name | JSON Key | Data Type | Description & Validation Rule |
| :--- | :--- | :--- | :--- | :--- |
| **A** | `Invoice_No` | `invoice_number` | `STRING` | Unique per tenant (e.g., `INV-2026-0042`). Required. |
| **B** | `Client_Name` | `client_name` | `STRING` | Legal or trade name of buyer. Required. |
| **C** | `Client_Email` | `client_email` | `STRING` | Valid email address for invoice dispatch. Required. |
| **D** | `Client_Phone` | `client_phone` | `STRING` | E.164 format (`+919876543210`) for WhatsApp/SMS link. Optional. |
| **E** | `Client_State` | `client_state` | `STRING` | Used to compute intra-state (`CGST+SGST`) vs inter-state (`IGST`). |
| **F** | `Item_Description` | `item_description` | `STRING` | Service or product summary (or JSON array reference for multi-line). |
| **G** | `Subtotal` | `subtotal_major` | `DECIMAL` | Base amount before tax in major currency (e.g., `2500.00`). |
| **H** | `Tax_Rate_Pct` | `tax_rate_pct` | `DECIMAL` | Percentage (e.g., `18.0` for 18% GST, `0` for exempt). |
| **I** | `Total_Amount` | `total_major` | `DECIMAL` | Formula `=G2+(G2*H2/100)`, verified by backend in minor units. |
| **J** | `Issue_Date` | `issue_date` | `DATE` | `YYYY-MM-DD` format. |
| **K** | `Due_Date` | `due_date` | `DATE` | `YYYY-MM-DD` format (`>= Issue_Date`). |
| **L** | `Status` | `status` | `ENUM` | `DRAFT`, `READY_TO_BILL`, `SENT`, `OVERDUE`, `PAID`, `CANCELLED`. |
| **M** | `Payment_Link` | `payment_url` | `URL` | The payable link: the tenant's `upi://pay?...` deep link (opens GPay/PhonePe pre-filled, zero fee). Empty for tenants without a UPI VPA — no hosted checkout is invented. |
| **N** | `PDF_Drive_URL` | `pdf_url` | `URL` | Auto-populated signed HTTPS link to the invoice PDF: `GET /api/v1/invoices/{tenant_id}/{invoice_number}/pdf?token=<hmac>`. Falls back to the server file path only when `SHEETFLOW_PUBLIC_BASE_URL` is unset. |
| **O** | `Paid_At` | `paid_at` | `DATETIME` | ISO-8601 timestamp written automatically by payment webhook. |
| **P** | `Last_Reminder` | `last_reminder_stage` | `STRING` | `NONE`, `T_MINUS_3`, `T_ZERO`, `T_PLUS_7`. |

## 2. Relational Database Schema (SQLite / PostgreSQL)

### 2.1. Table: `tenants` (Multi-Client Support for Your Agency)
Allows you to serve 5–50 small business clients from a single backend deployment.
```sql
CREATE TABLE tenants (
    id TEXT PRIMARY KEY,                  -- UUID v4 (e.g. 'tnt_01J...')
    business_name TEXT NOT NULL,
    tax_id TEXT,                          -- GSTIN / VAT / EIN
    home_state TEXT NOT NULL,             -- e.g. 'Haryana'
    currency_code TEXT NOT NULL DEFAULT 'INR',
    google_sheet_id TEXT NOT NULL,
    upi_vpa TEXT,                         -- e.g. 'merchant@okaxis' for zero-fee UPI QR
    owner_email TEXT,                     -- weekly cash-flow digest recipient (nullable)
    webhook_secret TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL              -- ISO-8601 UTC
);
```

### 2.2. Table: `invoices`
All monetary columns end in `_minor` and store integer paise/cents (`₹1,500.50` -> `150050`).
```sql
CREATE TABLE invoices (
    id TEXT PRIMARY KEY,                  -- UUID v4
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    sheet_row_number INTEGER NOT NULL,
    invoice_number TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE, -- SHA256(tenant_id + ':' + invoice_number + ':' + issue_date)
    client_name TEXT NOT NULL,
    client_email TEXT NOT NULL,
    client_phone TEXT,
    client_state TEXT,
    item_description TEXT NOT NULL,
    subtotal_minor INTEGER NOT NULL,      -- Integer paise/cents
    tax_rate_bps INTEGER NOT NULL,        -- Basis points (18.00% = 1800)
    cgst_minor INTEGER NOT NULL DEFAULT 0,
    sgst_minor INTEGER NOT NULL DEFAULT 0,
    igst_minor INTEGER NOT NULL DEFAULT 0,
    total_minor INTEGER NOT NULL,         -- subtotal_minor + cgst + sgst + igst
    issue_date TEXT NOT NULL,             -- YYYY-MM-DD
    due_date TEXT NOT NULL,               -- YYYY-MM-DD
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
```

### 2.3. Table: `audit_events` (Idempotency & Immutable Log)
```sql
CREATE TABLE audit_events (
    id TEXT PRIMARY KEY,
    invoice_id TEXT NOT NULL REFERENCES invoices(id),
    event_type TEXT NOT NULL,             -- 'INVOICE_GENERATED', 'EMAIL_SENT', 'REMINDER_T_MINUS_3', 'WEBHOOK_PAID'
    event_key TEXT NOT NULL UNIQUE,       -- Prevents duplicate email/WhatsApp dispatch
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

## 3. Deterministic Tax & Rounding Rules

> **Place of supply:** an empty `client_state` is treated as the supplier's own state (intra-state, CGST+SGST), matching what the invoice PDF prints. Required states must be filled in for genuine inter-state IGST billing.
1. **Minor Unit Conversion:** `subtotal_minor = round(Decimal(subtotal_major) * 100)`.
2. **Tax Split Logic (India GST Mode):**
   - If `tenant.home_state.lower() == invoice.client_state.lower()`:
     - `cgst_minor = round(subtotal_minor * (tax_rate_bps / 2) / 10000)`
     - `sgst_minor = cgst_minor`
     - `igst_minor = 0`
   - Else (Inter-state):
     - `cgst_minor = 0`, `sgst_minor = 0`
     - `igst_minor = round(subtotal_minor * tax_rate_bps / 10000)`
3. **Total Verification:** `total_minor = subtotal_minor + cgst_minor + sgst_minor + igst_minor`.

## 4. Webhook Payload Contract (Google Apps Script -> FastAPI)
`POST /api/v1/webhooks/sheet-row-ready`

**Authentication (required):** the request must include
`X-SheetFlow-Signature: hex(HMAC_SHA256(tenant.webhook_secret, raw_request_body))`.
The signature covers the exact bytes sent. Missing or wrong signatures return `401`.
`status` is constrained to `READY_TO_BILL`; the server owns every other transition.

Operator endpoints (`POST /api/v1/dunning/run`, `GET /api/v1/digest/{tenant_id}`) require
`X-SheetFlow-Admin-Token` matching the server's `SHEETFLOW_ADMIN_TOKEN` env var.
```json
{
  "tenant_id": "tnt_client01",
  "sheet_row_number": 14,
  "invoice_number": "INV-2026-014",
  "client_name": "Apex Industrial Supplies",
  "client_email": "accounts@apexsupplies.in",
  "client_phone": "+919812345678",
  "client_state": "Haryana",
  "item_description": "Monthly Inventory & Billing Automation Retainer (Nov 2026)",
  "subtotal_major": "3500.00",
  "tax_rate_pct": "18.00",
  "issue_date": "2026-11-06",
  "due_date": "2026-11-13",
  "status": "READY_TO_BILL"
}
```

## 6. Table: `job_leads` (job intake, human-approved)

Created by `src/jobs/store.py`. Populated only from official APIs/RSS; status is owned by
the operator, never by the fetcher.

```sql
CREATE TABLE job_leads (
    id               TEXT PRIMARY KEY,   -- sha256(canonical url | source:external_id)
    source           TEXT NOT NULL,      -- 'freelancer:invoice', 'rss:host'
    external_id      TEXT NOT NULL,
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    url              TEXT NOT NULL DEFAULT '',
    budget_min_minor INTEGER,
    budget_max_minor INTEGER,
    currency         TEXT NOT NULL DEFAULT '',
    posted_at        TEXT,
    score            INTEGER NOT NULL DEFAULT 0,
    capability       TEXT,               -- INVOICE_AUTOMATION | SPREADSHEET_CLEANUP | DATA_ENTRY | PAYMENT_FOLLOWUP | REPORTING
    reasons          TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'NEW',  -- NEW | APPROVED | SKIPPED
    discovered_at    TEXT NOT NULL
);
```

Identity is the canonical project URL, so the same project returned by two search queries
is stored once. Re-scans refresh the score but never overwrite an `APPROVED`/`SKIPPED`
decision.
