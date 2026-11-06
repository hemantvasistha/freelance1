# AGENT_STATE

Updated 2026-11-06. Read this before touching the repository.

## What this product is

SheetFlow: a micro-SaaS that turns a business's own Google Sheet rows into GST invoices
with PDF + payment link, emails them, follows up on unpaid ones, and writes PAID/OVERDUE
back into the sheet. Python 3.14 / FastAPI / SQLite modular monolith. The customer's
interface is their spreadsheet; there is no web dashboard, deliberately.

## Source of truth for status

- `docs/implementation/PROJECT_STATUS.md` — per-feature verification matrix.
- `docs/implementation/EXECUTION_BACKLOG.md` — ordered remaining work.
- `docs/LOAD_TEST_REPORT.md` — measured latency, bottleneck, stages not run.
- `docs/BACKUP_AND_RECOVERY.md` — backup procedure + executed restore drill.
- `docs/MASTER_PROJECT_AUDIT.md`, `docs/CUSTOMER_ONBOARDING.md`,
  `docs/OWNER_OPERATIONS.md`, `docs/PRICING_AND_LIMITS.md`.

## Completed and locally verified

- Tenant isolation via per-tenant operator tokens (SHA-256 at rest, 60 req/min); no
  global admin token in client scripts.
- Invoice creation with GST split, idempotency by `sha256(tenant:number:issue_date)`,
  `UNIQUE(tenant_id, invoice_number)`.
- PDF generation + HMAC-signed download links bound to tenant and invoice.
- Invoice email dispatch with separate delivery status (`SENT`/`DRY_RUN`/`FAILED`/`SKIPPED`).
- Payment verification: gateway signature, amount, currency, replay protection, link reuse.
- Append-only `payments` ledger (`UNIQUE(invoice_id, provider_payment_id)`).
- Admin-only manual UPI reconciliation with partial payments and unique reference enforcement.
- Reminder scheduler with stage idempotency, retry, stranded-`PENDING` recovery.
- Weekly digest with per-tenant/period claim (no duplicate digests).
- Sheet write-back outbox with explicit ack, scoped per tenant.
- Owner ops view at `/api/v1/admin/ops`; landing page at `/`.
- Production config preflight (`validate_runtime_settings`); `--serve` exits 2 on fatal gaps.
- Concurrency tests: 8 threads ingesting one row → 1 invoice, 2 audit events; 8 threads
  recording one payment reference → 1 payment row.
- Load harnesses and restore drill (`scripts/`), all executed.

## Test status

```
py app/.venv/Scripts/python.exe sheetflow_engine.py --self-test
# Ran 66 tests ... OK
py app/.venv/Scripts/python.exe scripts/backup_restore_drill.py   # match: true
```

No known failing test.

## Configured vs unverified services

| Service | Configured | Live-tested |
|---|---|---|
| SMTP | code + env names only | no (no credentials) |
| Razorpay | code + signature checks | no (no keys; `create_payment_link` response shape assumed) |
| Google Sheets | Apps Script is pull-based | no (never run inside Google) |
| HTTPS/Caddy | config drafted in `deploy/` | no (no host) |

## In progress

Nothing is half-written. The working tree is committed.

## Next exact task

Deploy to a host with Caddy, then execute, in order:
1. `systemctl` start, `curl /health` over HTTPS.
2. One real Apps Script row → invoice PDF + payment link appears in the sheet.
3. One real SMTP invoice email arrives; audit shows `SENT`.
4. Razorpay sandbox payment → webhook → invoice PAID → sheet flips on next sync.
5. Restore drill against the production database copy.
Then update `docs/LOAD_TEST_REPORT.md` (run soak Stage 6) and
`docs/implementation/PROJECT_STATUS.md` (move rows to PRODUCTION_VERIFIED).

## Traps

- Tests must set env inline; `.env` is not readable by tooling.
- `process_sheet_row_webhook` generates PDFs synchronously — 0.4–1.1 s per invoice under
  concurrency. Do not call it inside a tight loop in a request handler.
- `sheet_sync_outbox` rows are acknowledged by the Apps Script; do not delete unacked rows.
- Never reintroduce a global admin token into `apps_script/Code.gs`.
- SQLite connections must be closed explicitly (Windows blocks deletes on open handles).
