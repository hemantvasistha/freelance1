# CHANGELOG.md — Chronological Build & Context Log

All notable changes, completed milestones, and architectural decisions are documented in this file. Every AI coding session must append an entry before closing.

## [1.1.0] — 2026-11-06 (Unattended runs, card payments, job intake)

Everything below exists because the product only earns when nobody has to click a menu.

### Added
- **Unattended runner** (`src/dunning/runner.py`): `run_daily` dispatches reminders for
  every active tenant, `run_weekly_digest` emails each owner a cash-flow digest. Both are
  reachable as `--run-daily` / `--run-digest` (cron) or via the in-process loop
  `SHEETFLOW_SCHEDULER_ENABLED=true`, which runs at `SHEETFLOW_SCHEDULER_HOUR` and on
  Mondays, dispatched to a thread so SMTP never blocks the event loop. `/health` reports it.
- **Digest recipient**: `tenants.owner_email` (additive migration; existing ledgers get the
  column on startup), `--owner-email` at onboarding, `SHEETFLOW_OWNER_EMAIL` as fallback.
  Tenants with no address are reported as `SKIPPED` instead of silently vanishing.
- **Razorpay** (`src/reconciliation/razorpay.py`): `POST /api/v1/webhooks/razorpay` verifies
  `X-Razorpay-Signature` with `RAZORPAY_WEBHOOK_SECRET` and marks the invoice `PAID`,
  idempotently and with underpayment rejection. With `RAZORPAY_KEY_ID/SECRET` set, invoice
  creation also asks Razorpay for a hosted payment link; if that call fails the invoice keeps
  its zero-fee UPI link rather than failing.
- **Job intake** (`src/jobs/`): `--jobs-scan` reads the official Freelancer.com public API
  (no key) and any RSS feeds in `SHEETFLOW_JOB_FEEDS`, scores each listing against the one
  deliverable this code produces (plus budget floor and recency), dedupes by canonical URL,
  and stores leads as `NEW`. `--jobs-list`, `--jobs-approve`, `--jobs-skip` are the only
  ways a lead moves. There is no login, no HTML scraping, no auto-bidding.
- **Sales kit** (`OFFER.md`): the exact offer, price, pitch, objections, delivery checklist
  and the things not to promise.

### Changed
- README rewritten as an operator manual; the "7-file AI context system" section is gone
  along with `BUILD_PROMPT.md`, which only told an assistant how to re-read its own docs.
- `process_sheet_row_webhook` accepts an optional `payment_link_provider`; default behaviour
  is unchanged (UPI link).

### Verified
- **38 tests** (`pytest tests -q` and `--self-test`), including the new
  `test_scheduler_runner.py` (multi-tenant daily run, digest delivery/skip, legacy-schema
  migration), `test_job_intake.py` (RSS + API parsing, scoring, dedupe, approval gate) and
  `test_razorpay.py` (signature, parsing, PAID, replay, underpayment).
- Live `--jobs-scan` against Freelancer.com: 60 listings fetched, deduped, scored, stored.

### Fixed
- **Failed email delivery is now retried** (`retry_failed_reminders`). The audit row is
  written before the send (that is what prevents double-sends), so a retry updates that row
  rather than creating a second claim. `run_daily` retries yesterday's failures first, then
  dispatches today's, and stops after 3 attempts so a dead address cannot mail forever.
  `--retry-failed` runs it on demand.

### Still open
- WhatsApp is not implemented.
- Stock/inventory or Tally export: not built, and not needed for the first clients.

## [1.0.3] — 2026-11-06 (Purge: no fake rails, no demo data, no dead entrypoints)

### Removed — dead or dishonest code
- **`https://pay.sheetflow.in/checkout/...`**: the domain does not exist, so every "Pay now"
  link on every invoice 404'd. `payment_url` is now the tenant's real UPI deep link (or empty
  when the tenant has no VPA). No hosted checkout is invented. This also deleted the last use
  of the `quote` import and the "Online Payment Checkout Link" label in both PDF renderers.
- **`run_demo_pipeline` and `--demo`**: they hardcoded tenant `tnt_demo_001` and secret
  `sec_live_demo_2026` and defaulted to `sheetflow_demo.db`, so a deployer following the
  README seeded fake clients into whatever DB path was current. Gone from `src/main.py`,
  `sheetflow_engine.py`, the README quickstart and the manifest. A fresh install is empty.
- **Stale root artifacts**: `sheetflow_project/sheetflow.db` (0 rows, and its `tenants` table
  predated the `tenant_id` column — pointing a server at it would break), plus the root copies
  of `Changelog.md`, `Project Context (1).md`, `Prd.md`, `Readme.md`, `Build Prompt.md`,
  `Code.txt`, three `project_manifest*.json` files and `Sample_Tax_Invoice_INV-2026-014.pdf`.
  The canonical docs live in `sheetflow_project/` only.

### Changed
- Reminder bodies end with `_payment_instruction(invoice)` instead of interpolating a URL that
  could be empty, so a VPA-less tenant still gets a coherent, payable message.
- `--db` now defaults to `sheetflow.db` everywhere (the demo default was the production-looking
  `sheetflow_demo.db`).

### Verified
- 15/15 tests still pass; `compileall` clean; no reference to `pay.sheetflow.in`,
  `run_demo_pipeline`, `--demo` or `sheetflow_demo` remains outside this changelog.

## [1.0.2] — 2026-11-06 (Revenue path: real email delivery, openable PDF links, tenant onboarding CLI)

These are the code gaps the business roadmap assumed were already closed.

### Added
- **Email delivery actually exists now** (`src/notifications/mailer.py`). `DRY_RUN=true` (default) routes every reminder to a `RecordingMailer` that keeps the rendered message and reports `DRY_RUN`; setting `DRY_RUN=false` builds an `SmtpMailer` (STARTTLS, `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASSWORD`). Requesting live delivery without `SMTP_PASSWORD` raises `MailerNotConfigured`, which `/dunning/run` surfaces as `503` instead of silently dropping reminders. `ReminderAction` now carries `delivery_status` / `delivery_error`, and `/health` reports `email_delivery`.
- **Invoice PDFs are now client-openable** (`src/billing/document_links.py`, `GET /api/v1/invoices/{tenant_id}/{invoice_number}/pdf?token=…`). The sheet's PDF column used to receive a Windows path (`../_smoke/invoices\INV-1.pdf`) that nobody could open. Links are now HTTPS URLs signed with an HMAC derived from the tenant's `webhook_secret` — unguessable, tenant-scoped, and non-expiring so old invoices keep working. `pdf_url` is returned by the webhook and written to column N by `Code.gs`.
- **Tenant onboarding CLI** (`src/onboarding.py` + `sheetflow_engine.py --create-tenant / --list-tenants`). Onboarding client #2 no longer needs a Python snippet: the command prints a ready-to-paste `SHEETFLOW_CONFIG` block, the generated webhook secret, and whether an existing secret was rotated (which silently invalidates a live sheet).
- **Apps Script retry** (`fetchWithRetry` in `Code.gs`): 3 attempts with 1s/2s/4s backoff on 429/5xx/network errors. Safe because invoice creation is idempotent server-side.
- `currency_symbol()` in `src/schemas.py`, replacing four duplicated `₹ if INR else CODE` ternaries.

### Verified
- **15/15 tests** (`python sheetflow_engine.py --self-test`), including new `tests/test_onboarding_and_mail.py` (onboarding, secret rotation, mailer selection, PDF-token tamper rejection) and a PDF-download assertion in the HTTP integration test.
- Live uvicorn run: signed webhook → `pdf_url`, `GET` that URL → `200 application/pdf` / `%PDF-1.3`, same URL without `?token` → `401`; `/dunning/run` → `delivery_status: DRY_RUN`, re-run same day → `dispatched_count: 0` (no double-send).
- `--create-tenant` / `--list-tenants` exercised against a scratch database.

### Still open
- WhatsApp is **not** implemented (the PRD mentions it; nothing sends it).
- Payment reconciliation still verifies SheetFlow's own HMAC, not Razorpay/Stripe payloads.
- A `FAILED` email delivery is recorded and surfaced but does not auto-retry, because the audit claim is written before sending to prevent double-sends.
- No scheduling: dunning and the digest are invoked by the Apps Script menu (or cron), not by an internal scheduler.

## [1.0.1] — 2026-11-06 (Repair pass: environment setup, auth, Pydantic v2, PDF fallback)
### Fixed — the suite could not even be imported on a current Pydantic
- **Pydantic v2 migration (`src/schemas.py`)**: `@validator`/`@root_validator` are now `@field_validator`/`@model_validator(mode="after")`. The old decorators raise `PydanticUserError: ... you MUST specify skip_on_failure=True` on import, which failed all 5 test modules on Pydantic 2.14.
- **PDF rendering actually works** (`src/billing/pdf_generator.py`): WeasyPrint raised `OSError: cannot load library 'libgobject-2.0-0'` on a stock Windows box, so every invoice render crashed. The HTML template is still written, and PDF output now falls back to a ReportLab renderer with a Unicode font probe so `₹` renders. `active_pdf_engine()` reports the backend used and `/health` exposes it.

### Fixed — security
- **Sheet-row webhook is authenticated**: requests must carry `X-SheetFlow-Signature`, a hex HMAC-SHA256 of the exact request body keyed with the tenant's `webhook_secret` (`compute_sheet_signature`). Previously anyone could create invoices for any tenant. `Code.gs` now computes and sends this header.
- **Callers can no longer self-declare invoice status**: `SheetRowWebhookPayload.status` is constrained to `READY_TO_BILL`, so a forged payload cannot create an already-`PAID`/`CANCELLED` invoice.
- **Operator endpoints require `SHEETFLOW_ADMIN_TOKEN`** (`X-SheetFlow-Admin-Token`): `/api/v1/dunning/run` and `/api/v1/digest/{tenant_id}` returned any tenant's receivables to anonymous callers; they now return 503 when no token is configured and 401 otherwise.

### Fixed — correctness
- **Dunning catch-up** (`src/dunning/scheduler.py`): stages now cover windows (`-3..-1`, `0..6`, `>=7`) instead of exact days, so a missed cron run no longer drops the T-3 or T-0 reminder permanently. Messages adapt ("due in N day(s)", "was due on …").
- **Place of supply**: a blank `client_state` is treated as intra-state (matching what the invoice PDF prints) instead of silently charging IGST.
- **Audit idempotency is atomic**: `record_audit_event_if_new` uses `INSERT OR IGNORE` + `rowcount` instead of a racy SELECT-then-INSERT. `create_or_get_invoice` recovers from a concurrent `IntegrityError` and returns the existing invoice instead of a 500.
- **SQLite robustness** (`src/database.py`): `init_db` no longer leaks a connection (`closing`), and connections enable WAL, `synchronous=NORMAL` and `busy_timeout=5000` so webhook writes and digest reads stop colliding with "database is locked".
- **Cancelled invoices cannot be marked PAID**; `/digest` defaults to today instead of a hardcoded 2026-11-06; unknown tenants return 404 instead of 500.
- **`Code.gs` menu no longer throws**: `triggerDunningRun` and `requestWeeklyDigest` were referenced by `onOpen()` but never defined.

### Changed
- Root `Sheetflow Engine.py` (a 1,900-line copy of the package plus an inlined test suite) is now `sheetflow_engine.py`, a thin launcher over `sheetflow_project/` — one copy of the code, and `--self-test` tests the real package. `--demo`, `--self-test`, `--serve` all work.
- Added `requirements.txt`, `.env.example` and `.gitignore` (pinned install path, secret template, and no committing of `*.db`).

### Verification
- `python sheetflow_engine.py --self-test` → **9/9 tests pass** (Python 3.14, FastAPI 0.162, Pydantic 2.14, WeasyPrint 70 unavailable → ReportLab path exercised).
- `python sheetflow_engine.py --demo` → generated `INV-2026-014.pdf` (`%PDF-1.3`) and the weekly digest.
- Live HTTP smoke test (`uvicorn src.main:app`): `/health` 200 with `pdf_engine: reportlab`; unsigned webhook 401; correctly signed webhook 200 and idempotent (`created_new=false`); digest without token 401.

## [1.0.0] — 2026-11-06 (Full Block-by-Block Build, Integration & Reinspection)
### Added & Verified
- **Block 1 (`M1_CORE_SCHEMA_AND_CONFIG`)**: Implemented `src/config.py`, `src/database.py`, `src/models.py`, and `src/schemas.py` with `ROUND_HALF_UP` integer minor-unit (`paise`/`cents`) arithmetic, intra-state (`CGST + SGST`) vs inter-state (`IGST`) splits, and `SHA256` idempotency keys. Verified via `tests/test_block1_core.py` (5/5 tests passing).
- **Block 2 (`M2_PDF_AND_PAYMENT_LINKS`)**: Implemented `src/billing/payment_links.py` (NPCI UPI deep-link + checkout URL generator) and `src/billing/pdf_generator.py` (A4 HTML-to-PDF renderer via WeasyPrint). Verified via `tests/test_block2_billing.py` and visual inspection of `Sample_Tax_Invoice_INV-2026-014.pdf`.
- **Block 3 (`M3_GOOGLE_SHEETS_BRIDGE`)**: Implemented `Code.gs` custom Google Sheets menu (`⚡ SheetFlow Billing`) and `src/ingestion/sheet_sync.py`. Verified via `tests/test_block3_ingestion.py`.
- **Block 4 (`M4_DUNNING_AND_RECONCILIATION`)**: Implemented `src/dunning/scheduler.py` (`T-3`, `T+0`, `T+7` escalation + Monday 8:00 AM Cash-Flow Digest) and `src/reconciliation/webhooks.py` (`hmac.compare_digest` signature verification & automatic `PAID` reconciliation). Verified via `tests/test_block4_dunning_recon.py`.
- **Combined Production Package (`sheetflow_engine.py`)**: Combined all 4 blocks into a unified FastAPI + CLI runner and verified full HTTP lifecycle via `tests/test_integration_all.py` (9/9 total tests passing in 0.97s).

## [0.1.0] — 2026-11-06
### Added
- **`PRD.md`**: Defined the B2B "Financial Plumbing" product thesis, SME pain points (saving 8.5 hrs/month and 60–80% of processing costs per [Winvesta](https://www.winvesta.in/blog/freelancers/how-to-set-up-automated-invoicing-for-freelancers-a-simple-guide-that-saves-hours)), freelance rate benchmarks ($13/hr median for basic Google Sheets vs. $30–$60/hr for Python/AI workflow automation per [Upwork](https://www.upwork.com/hire/google-spreadsheet-freelancers/cost/), [Upwatcher](https://www.upwatcher.io/market/python/), and [Jahanzaib](https://www.jahanzaib.ai/blog/ai-automation-consultant-hourly-rate)), and the 4-module MVP scope.
- **`PROJECT_CONTEXT.md`**: Locked the tech stack (Google Sheets UI + Python FastAPI + SQLite + Jinja2 PDF + Google Apps Script) and established the persistent AI memory rules.
- **`BUILD_PROMPT.md`**: Created copy-paste prompts for Session Initialization, Client Customization, and Context Checkpointing so AI assistants never lose state across sessions.
- **`DATA_MODEL.md`**: Specified the 16-column Google Sheets ledger contract, SQLite tables (`tenants`, `invoices`, `audit_events`), integer minor-unit currency rules, and GST split logic.
- **`project_manifest.json`**: Initialized the machine-readable milestone tracker with `M0_CONTEXT_AND_SPECS` marked `completed` and `M1_CORE_SCHEMA_AND_CONFIG` marked `next_up`.
- **`README.md`**: Added quick-start instructions, AI context workflow, and the client pitch/pricing guide.

### Next Session Action (`M1_CORE_SCHEMA_AND_CONFIG`)
- Paste Section 1 of `BUILD_PROMPT.md` into your AI coding editor to generate `src/config.py`, `src/database.py`, `src/models.py`, and `src/schemas.py`.
