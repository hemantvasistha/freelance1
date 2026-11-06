# SheetFlow — implementation status

Last verified: 2026-11-06. Every claim below is tied to a file and a test that exists in
this repository. Anything not executed is listed as unverified, not as working.

## Verification legend

`VERIFIED_IN_TEST` — exercised by the automated suite against real SQLite state.
`IMPLEMENTED_UNVERIFIED` — code exists, but the external service was never called.
`BLOCKED` — needs a credential/account/host this environment does not have.
`NOT_IMPLEMENTED` — deliberately absent.

## Verification matrix

| Feature | Status | Evidence |
|---|---|---|
| Tenant isolation | VERIFIED_IN_TEST | per-tenant operator token hashed with SHA-256; `tests/test_tenant_security_and_sheets.py` |
| Invoice creation + GST split | VERIFIED_IN_TEST | `src/schemas.py`, `tests/test_block1_core.py`, `test_block3_ingestion.py` |
| Invoice idempotency | VERIFIED_IN_TEST | `idempotency_key = sha256(tenant:number:issue_date)`, duplicate ingest test |
| PDF generation | VERIFIED_IN_TEST | `src/billing/pdf_generator.py`, asserts `%PDF-` header |
| PDF access control | VERIFIED_IN_TEST | signed token bound to tenant + invoice; tamper returns 401 |
| Invoice email dispatch | VERIFIED_IN_TEST (dry-run) | `src/billing/invoice_email.py`; `INVOICE_EMAIL` audit event |
| Live SMTP delivery | BLOCKED | needs `SMTP_USER`/`SMTP_PASSWORD` |
| Payment signature + amount + currency | VERIFIED_IN_TEST | `src/reconciliation/webhooks.py`, `razorpay.py` |
| Payment history ledger | VERIFIED_IN_TEST | `payments` table, `test_payments_and_guards.py` |
| Manual UPI reconciliation | VERIFIED_IN_TEST | `POST /api/v1/admin/manual-payment`, partial-payment + unique-reference tests |
| Razorpay link creation | IMPLEMENTED_UNVERIFIED | `create_payment_link` never called against the real API (no keys) |
| Payment-link reuse | VERIFIED_IN_TEST | `test_reingesting_a_row_creates_only_one_gateway_link` |
| Reminder scheduling | VERIFIED_IN_TEST | `src/dunning/scheduler.py`, stage idempotency via `audit_events` |
| Stranded reminder recovery | VERIFIED_IN_TEST | stale `PENDING` claim retried once |
| Digest duplicate suppression | VERIFIED_IN_TEST | `notification_deliveries` claim table |
| Sheet write-back (PAID/OVERDUE) | VERIFIED_IN_TEST (server side) | `sheet_sync_outbox` + `/api/v1/operator/sheet-updates` |
| Real Google Sheets execution | BLOCKED | write-back is pull-based from Apps Script; never run against a live sheet |
| Owner ops view | VERIFIED_IN_TEST | `GET /api/v1/admin/ops` authenticates, returns real counts |
| Landing page | VERIFIED_IN_TEST + live curl | served at `/`, 200 `text/html` |
| Production config guard | VERIFIED_IN_TEST | `validate_runtime_settings`, refuses missing URL/admin token |
| Deployment under Caddy/TLS | BLOCKED | needs a host; `deploy/` is untested in practice |
| Customer web dashboard | NOT_IMPLEMENTED | the customer's interface is their own Google Sheet |
| Plan enforcement / subscription billing | NOT_IMPLEMENTED | one plan, billed manually |
| Line items, credit notes, refunds, partial refunds | NOT_IMPLEMENTED | single-rate invoices only |
| PostgreSQL, object storage, job queue | NOT_IMPLEMENTED | SQLite + in-process scheduler suffice for the target size |

## Measured performance (local, 2026-11-06)

Harness: `scripts/load_test.py`, in-process FastAPI TestClient, SQLite file (WAL) in a
temp directory, Python 3.14.7, 4 tenants / 40 invoices / 150 reads.

| Operation | p50 | p95 | max |
|---|---|---|---|
| Invoice ingest incl. PDF | 35.6 ms | 54.1 ms | 1389.2 ms |
| `GET /health` | 19.5 ms | 33.0 ms | 207.1 ms |
| `GET /api/v1/admin/ops` (5 queries) | 31.3 ms | 45.4 ms | 102.4 ms |

The 1.4 s maximum on ingest is PDF engine warm-up on the first invoice (WeasyPrint falls
back to ReportLab on this machine), not a steady-state cost. **This is not a production
capacity measurement**: no network, no TLS, no reverse proxy, no second process, no
concurrent clients. Re-run after any change to the ingest path.

## Test suite

```bash
py app/.venv/Scripts/python.exe sheetflow_engine.py --self-test
# Ran 63 tests ... OK
```

## Blockers

See `BLOCKERS` section below; all are external, none are code gaps.

1. **SMTP credentials** — live invoice/reminder email delivery.
2. **Razorpay account + keys** — any real payment link or webhook.
3. **Google account** — running `Code.gs` against a real spreadsheet.
4. **Host + domain** — HTTPS deployment, Caddy config, backup restore drill.

## Blockers detail and what unblocks each

| Blocker | What is needed | What becomes verifiable |
|---|---|---|
| SMTP | `SMTP_USER`, `SMTP_PASSWORD`, `DRY_RUN=false` | invoice email actually delivered, bounce handling |
| Razorpay | `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, webhook secret | real link creation, real webhook reconciliation |
| Google | a test spreadsheet + paste `Code.gs` | row ingestion from the sheet, PAID write-back round trip |
| Host | a VPS + domain | TLS, health over HTTPS, backup/restore |

## Changelog of this work

- `adda3f2` — tenant-scoped operator tokens (removed the global admin token from client
  scripts), invoice email dispatch with delivery status, PAID/OVERDUE write-back outbox,
  stranded-reminder recovery.
- `fb59006` — payment-link reuse, currency validation, owner ops view, landing page,
  Apps Script triggers.
- (this change) — append-only `payments` ledger, admin-only manual UPI reconciliation
  with partial payments, digest duplicate suppression, production config guard,
  measured load harness, status documentation.

## Pre-deployment verification (executed 2026-11-06)

`scripts/verify_deployment.py` runs the customer path and every authorization boundary
against a live origin and prints PASS / BLOCKED / FAIL with a non-zero exit on failure.
It was run against a **production-configured local instance**:
`SHEETFLOW_ENV=production`, `DRY_RUN=false`, real 43-char admin token, real per-tenant
operator token, no SMTP password.

Result: **14 passed, 3 blocked, 0 failed**, 0 HTTP 5xx in the server log.

Passed: landing page, unsigned webhook 401, forged signature 401, malformed body 422,
operator endpoint 401, manual payment without admin token 401, signed row creates invoice
(`delivery=SKIPPED` honestly reported), replay idempotent, PDF 401 without token and a
real 23,979-byte `%PDF-` with token, manual payment marks PAID, write-back queued and
acknowledged, owner ops view with real aggregates.

Blocked (credential-gated, not failures): TLS termination (served over plain HTTP),
live email delivery, Razorpay webhook signature (secret unset).

Two real defects were found and fixed by this run: the landing page shipped a
`hello@example.com` contact placeholder (it now renders `SHEETFLOW_OWNER_EMAIL`, or an
honest onboarding fallback), and `deploy/README.md` told operators to expect 401 where
the API correctly returns 422 for a malformed body.

Production preflight was also exercised: `SHEETFLOW_ENV=production` with an empty public
base URL and admin token exits with code 2 and names both problems.

## Next exact task

Provision a host + domain, install Caddy, and run `scripts/verify_deployment.py` against
the HTTPS origin - then repeat it after each deploy. The code is deployable today; what is
missing is the host, the DNS record, and the SMTP/Razorpay credentials. `caddy` is not
installed on this workstation, so TLS itself has never been exercised here.
