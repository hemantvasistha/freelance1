# Execution backlog

Ordered by what actually blocks revenue. Nothing here is invented scope: each item is
justified by either a defect, a security gap, or a paying-customer workflow.

## P0 — blocks first revenue (external credentials required)

### 1. Live SMTP delivery round trip
- **Why:** invoice email is implemented but has never left the machine. `DRY_RUN=true`
  is the only state ever executed.
- **Needs:** `SMTP_USER`, `SMTP_PASSWORD`, `DRY_RUN=false`.
- **Acceptance:** one invoice email arrives at a real inbox; `INVOICE_EMAIL` audit event
  shows `SENT`; a deliberately wrong password produces `FAILED` and the invoice still
  exists.
- **Status:** BLOCKED.

### 2. Razorpay sandbox link + webhook
- **Why:** `create_payment_link` is untested against the real API; its response shape
  (id, short_url) is assumed.
- **Needs:** sandbox key pair + webhook secret.
- **Acceptance:** link created, URL opens a real checkout; a signed `payment.captured`
  webhook marks the invoice PAID; replaying it does not duplicate the payment row.
- **Status:** BLOCKED.

### 3. Real Google Sheets round trip
- **Why:** `Code.gs` has never run inside Google. The 16-column contract and the
  invoice-number matching in `syncPaymentStatuses` are unverified against a real sheet.
- **Needs:** a test spreadsheet, Apps Script paste, one READY_TO_BILL row.
- **Acceptance:** row produces a PDF + payment link; a manual payment later flips the
  row to PAID with date and reference via the 6-hourly trigger.
- **Status:** BLOCKED.

### 4. HTTPS deployment + restore drill
- **Why:** `deploy/` has never been executed; backup/restore is documented, not proven.
- **Needs:** VPS + domain.
- **Acceptance:** `/health` over HTTPS; `sqlite3 .backup` restored into a scratch path
  with matching invoice counts; service restarts cleanly.
- **Status:** BLOCKED.

## P1 — correctness and operations

### 5. Progress-based reminder backoff instead of fixed stages
- **Why:** three fixed stages exist. A customer who ignores them gets no further
  follow-up, which is where collections value actually accrues.
- **Acceptance:** configurable intervals, `max_reminders` per invoice, suppression on
  PAID/CANCELLED, tests for each rule.
- **Status:** NOT_STARTED (needs a customer's real behaviour to pick defaults).

### 6. Owner alerting on exceptions
- **Why:** `/api/v1/admin/ops` reports exceptions only when someone looks.
- **Acceptance:** a cron job that emails the owner when `needs_attention` is true, at
  most once per day, with the failing counts.
- **Status:** NOT_STARTED (cheap; do it at deployment time).

### 7. Invoice numbering owned by the server
- **Why:** numbers come from the sheet. Two rows with the same number are rejected by
  `UNIQUE(tenant_id, invoice_number)` — safe, but the customer's own numbering can skip
  or collide with no explanation.
- **Acceptance:** optional server-assigned sequential numbers per tenant, gapless within
  a financial year, concurrency-safe.
- **Status:** NOT_STARTED.

## P2 — only with observed demand

### 8. Line items and multi-rate invoices
Blocked on a real customer needing more than one line. Today the schema is one
description + one tax rate.

### 9. Plan enforcement (₹999 monthly, invoice cap)
Only worth building when a customer actually exceeds a limit. Currently a contract, not
code — stated plainly in `docs/PRICING_AND_LIMITS.md`.

### 10. Customer web dashboard
The sheet is the interface and the customer already lives there. Build only if a paying
customer refuses to use the sheet.

### 11. PostgreSQL / object storage / worker queue
Trigger: a second app process, or a host whose disk is not durable. Until then SQLite +
the in-process scheduler is lower total cost of ownership.

## Explicitly rejected scope

- WhatsApp reminders (needs a provider account and a messaging template approval).
- Automatic bank-statement matching (no reliable rail for UPI intents).
- Tally/Zoho export (different product).
- Freelancer job intake inside the product surface (kept as a CLI tool, not sold).
