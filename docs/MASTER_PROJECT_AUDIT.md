# SheetFlow — Master Audit & Product Decision

Evidence in this document comes from reading the source in this repository. Market
claims are marked **hypothesis** and have not been validated with customers.

## 1. What the software genuinely does today

A single Python/FastAPI process (`sheetflow_engine.py --serve`) that:

1. Accepts a **signed** `POST /api/v1/webhooks/sheet-row-ready` from a customer's Google
   Apps Script and creates an invoice with a deterministic GST split (CGST/SGST vs IGST)
   in integer minor units (`src/schemas.py`).
2. Renders an A4 invoice to HTML/PDF (`src/billing/pdf_generator.py`) and builds a
   zero-fee UPI intent link (`src/billing/payment_links.py`).
3. Serves the PDF over a signed URL, bound to tenant + invoice number
   (`src/billing/document_links.py`, `src/main.py`).
4. Emails the invoice to the client and records whether delivery actually happened
   (`src/billing/invoice_email.py`, added in this pass).
5. Runs a T-3 / T+0 / T+7 reminder schedule, idempotent per stage via `audit_events`
   (`src/dunning/scheduler.py`), plus a Monday cash-flow digest.
6. Reconciles payments from a signed webhook or a Razorpay webhook, refusing
   underpayments (`src/reconciliation/`).
7. Queues PAID/OVERDUE changes for the customer's sheet and acknowledges delivery
   (`src/sheets/outbox.py`, added in this pass).
8. Scrapes Freelancer.com's public API/RSS and drafts pitch emails
   (`src/jobs/`, `sheetflow_engine.py --pitch-jobs`).

Working end-to-end: 1–7. Item 8 is a lead-generation side tool, not part of the
customer product.

## 2. Feature inventory and verdicts

| Feature | Verdict | Reason |
|---|---|---|
| Sheet-row webhook → invoice | **RETAIN** | The core promise. Signed, idempotent, tested. |
| GST split (CGST/SGST vs IGST) | **RETAIN** | Deterministic, in minor units, validated by tests. |
| PDF invoice rendering | **RETAIN** | The artifact the customer shows their client. |
| Signed PDF links | **RETAIN** | Removes the "server path the customer can't open" bug. |
| UPI intent link | **RETAIN** | Zero-fee collection; the differentiator vs a plain invoice tool. |
| Invoice email dispatch | **REPAIR (done)** | Previously only a PDF was produced; delivery was never attempted. |
| Reminder schedule | **RETAIN** | The retention feature: "it chases for me". |
| Stranded-claim recovery | **REPAIR (done)** | A crashed worker could strand a reminder forever, silently. |
| Payment reconciliation | **RETAIN** | Signature, amount, and replay checks already correct. |
| Razorpay payment links | **DEFER** | Duplicate-link risk on re-ingest; UPI covers the target segment. |
| Sheet write-back of PAID/OVERDUE | **REPAIR (done)** | Ledger drifted from server state with no recovery path. |
| Tenant operator token | **REPAIR (done)** | Client scripts carried the server-wide admin token. |
| Weekly digest | **RETAIN** | Cheap, visible value; drives the monthly renewal conversation. |
| Freelancer/job intake + pitch drafts | **DEFER** | No customer pays for it. Keep out of the product surface. |
| Razorpay hosted checkout | **DEFER** | Only matters once a customer asks for card payments. |
| PostgreSQL, queues, S3 | **DEFER** | SQLite on one VPS serves the first 10 customers. |
| Dashboard UI | **DEFER** | The customer's interface is their own sheet. |

## 3. Defects found and corrected in this pass

| # | Location | Defect | Impact | Fix | Test |
|---|---|---|---|---|---|
| 1 | `apps_script/Code.gs`, `src/onboarding.py` | Every client's sheet script held the server's global `ADMIN_TOKEN`, which unlocks every tenant's dunning and digest | One customer could read/trigger another's receivables | Removed from client script and onboarding output; each tenant gets its own hashed operator token | `test_code_gs_never_references_the_server_admin_token`, `test_apps_script_config_carries_only_the_tenant_token` |
| 2 | `src/main.py` | No tenant-scoped operator endpoint existed | Tenant actions needed the global token | Added `require_operator_tenant` + `/api/v1/operator/*`; tenant derived from token, body `tenant_id` ignored | `test_operator_token_only_reaches_its_own_tenant`, `test_missing_admin_and_foreign_operator_tokens_are_rejected`, `test_digest_is_scoped_to_the_token_tenant` |
| 3 | `src/ingestion/sheet_sync.py` | Invoice "sent" meant only "PDF written to disk" | Owner believed clients were invoiced when nothing was emailed | Real email dispatch with a separate `delivery_status` and `INVOICE_EMAIL` audit event | `test_invoice_email_is_dispatched_and_recorded_separately` |
| 4 | `src/reconciliation/*` | Payment state changed server-side but never reached the sheet | Ledger showed SENT forever; owner chased paid customers | Outbox keyed on `invoice_number`, pulled and acknowledged by the sheet | `test_paid_status_is_queued_then_acknowledged`, `test_replayed_payment_does_not_queue_a_second_update` |
| 5 | `src/dunning/scheduler.py` | Only `FAILED` reminders were retried; killed processes left `PENDING` claims forever | Silent lost reminders | Stale `PENDING` claims become retryable; no duplicate stage send | `test_pending_claim_from_a_crashed_run_is_retried` |
| 6 | `src/dunning/scheduler.py` (introduced and fixed in this pass) | Retry query did not select `created_at` | Recovery path crashed | Column added to the query | Covered by test 5 |

## 4. Security position (verified, not assumed)

- Webhook authenticity: HMAC-SHA256 over the exact body with the tenant's own secret;
  compared with `hmac.compare_digest`.
- Operator authenticity: per-tenant token, stored only as SHA-256; 60 req/min per token.
- Tenant isolation: since the tenant is derived from the credential, a body-supplied
  `tenant_id` cannot select another tenant. Tested for digest, dunning, and outbox ack.
- Invoice PDF links: HMAC bound to tenant + invoice number; tampering returns 401.
- Payment integrity: signed payload, amount floor, replay-safe `event_key`; a caller
  cannot post `status: PAID` to the ingestion endpoint (422).
- **Not yet mitigated:** no TLS termination in-process (Caddy does it), no per-IP
  throttling at the edge, operator tokens do not expire (rotation = re-onboard).

## 5. Known limitations (do not advertise past these)

- GST: only intra/inter-state CGST+SGST/IGST at a single rate; no HSN/SAC, no credit
  notes, no refunds, no composition/exempt handling beyond `tax_rate_pct = 0`. This is
  arithmetic, not statutory compliance.
- Payments: UPI intent links cannot be reconciled automatically; reconciliation needs
  Razorpay or manual entry. No partial payments despite a `PARTIALLY_PAID` idea in docs.
- Reminder retries: recovery of a `PENDING` claim can duplicate an email if the worker
  died *after* SMTP accepted it. Documented, bounded to one extra send.
- Sheets: write-back is pull-based (menu item or time trigger); the server has no Google
  credentials and cannot push.
- Razorpay payment-link creation is not idempotent; keep it disabled until fixed.
- One SQLite file, one process. Fine for a handful of clients, not for concurrency.

## 6. Business model

**Chosen segment (hypothesis):** small Indian service businesses that invoice 20–200
times a month from a spreadsheet and chase payment by phone — coaching centres,
clinics, distributors, small agencies.

| Segment | Frequency | Manual cost | Willingness to pay | Reachability | Onboarding | Verdict |
|---|---|---|---|---|---|---|
| Coaching centres | Monthly fees, high volume | High (parent chasing) | Medium | Local, referrals | Low (sheet already exists) | **Primary** |
| Clinics | Monthly patient billing | Medium | Medium | Local | Low | Secondary |
| Distributors | 40–200 party invoices | High | Medium-high | Trade networks | Medium | Secondary |
| Freelancers | 5–15 retainers | Low-medium | Low | Online | Low | Poor (low urgency) |

**Promise:** "SheetFlow turns the invoice rows you already keep in Google Sheets into
GST-correct PDF invoices with a tap-to-pay UPI link, and chases the payment for you."

**Pricing to test:** ₹999/month (single business, unlimited invoices, reminders,
digest) + ₹2,000 one-time setup. At ₹999, 1–5 customers reaches ₹1,000–₹5,000 MRR.
Setup fee exists to price the founder's onboarding hour, not as a profit centre.

## 7. Operating economics (assumptions, not measurements)

| Item | Assumption | Monthly |
|---|---|---|
| VPS (1 vCPU, 1 GB, Ubuntu) | ₹300–600 | ₹450 |
| Domain | ₹800/yr | ₹70 |
| SMTP (Gmail app password / low-volume provider) | free–₹200 | ₹100 |
| Razorpay | 2% + GST per transaction | variable |
| Backups | same VPS disk + weekly off-box copy | ₹0 |
| **Total fixed** | | **≈₹620/month** |

Break-even is one customer at ₹999. Owner workload after stabilisation is estimated at
1–3 hours/week **if** no integration fails: onboarding (~45 min/customer), exception
triage, monthly backup restore check. This is an estimate, not a measurement.

## 8. Roadmap from here (ordered)

1. **P0 remaining:** rotate operator tokens on demand; add per-IP rate limiting at Caddy.
2. **P1 remaining:** make Razorpay link creation idempotent (store the link per invoice)
   or remove the provider path; validate currency codes as an enum.
3. **P1:** time-driven Apps Script trigger for `syncPaymentStatuses` so write-back is
   automatic instead of menu-driven.
4. **P2:** PostgreSQL only when a second process or >10 tenants appears; object storage
   only when two app instances must share PDFs.
5. **P2:** credit notes / partial payments, only after a paying customer asks.
6. **Deferred indefinitely:** dashboard, job intake in the product surface, WhatsApp.

## 9. Validation experiment (not yet run)

1. List 20 local businesses matching the primary segment.
2. Ask each what they do today when an invoice is unpaid (5-minute conversation).
3. Show the PDF + UPI link on a phone using synthetic data.
4. Ask for ₹2,000 setup to onboard their real sheet.
5. Record: demos, paid conversions, time-to-first-invoice, support minutes per customer.

Success for the first milestone: **two paying customers and one renewal**. Anything
else is data, not failure.

## 10. Verification status of this pass

- `python sheetflow_engine.py --self-test`: 48 tests, all passing after the fixes above.
- Verified locally with SQLite and `DRY_RUN=true`.
- **Not verified:** live SMTP delivery, live Razorpay webhooks, real Google Apps Script
  execution, and production TLS/reverse proxy behaviour. These require credentials and
  a deployment that this environment does not have.
