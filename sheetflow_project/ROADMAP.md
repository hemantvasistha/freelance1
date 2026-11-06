# SheetFlow — Merged Roadmap: Code State × Client Acquisition × Revenue

> **Verified 2026-11-06** against the actual repository, not against notes.
> Environment: Python 3.14.7, FastAPI 0.162, Pydantic 2.14, WeasyPrint 70 (unavailable on
> this Windows box → ReportLab path), SQLite.
> Test state: **15/15 passing** (`python sheetflow_engine.py --self-test`).
> This file merges three inputs: the business roadmap (Phase 0–3), the earlier repair log
> (v1.0.1), and the revenue-path work (v1.0.2). Where the business roadmap was written
> against assumptions that never matched the code, the correction is called out below.

---

## 0. Compatibility audit of the business roadmap

| Roadmap claim | Reality | Status |
|---|---|---|
| "The code works; the Pydantic issue only blocks tests, not usage" | False. `src/main.py` imports `src/schemas`, so the server also failed to import. Nothing ran. | Fixed in v1.0.1 |
| Fix = add `skip_on_failure=True` at `schemas.py:130`, migrate `@validator` at `:123` | Done properly with `@field_validator`/`@model_validator`; those line numbers no longer exist. | Done |
| "`sheetflow_engine.py` missing → rename `src/main.py` to the project root" | Renaming `main.py` would break `from src.*` imports. It is now a thin launcher that puts `sheetflow_project/` on `sys.path`. | Done |
| "Add stub `triggerDunningRun()` / `requestWeeklyDigest()`" | Stubs alone would now fail: those endpoints require `X-SheetFlow-Admin-Token`. Real implementations with auth were added instead. | Done |
| "`format_major_currency` hardcodes ₹" | Wrong — it takes a symbol. The real duplication was four `₹ if INR else CODE` ternaries, now one `currency_symbol()`. | Fixed in v1.0.2 |
| "Set `DRY_RUN`, `SMTP_*` for email dispatch" | Those settings existed but **nothing read them**; no email was ever sent. Now real (`RecordingMailer` / `SmtpMailer`). | Fixed in v1.0.2 |
| "PDF path vs Drive URL" | Real gap: the sheet received `generated_invoices\INV-1.pdf`. Now a signed HTTPS URL is served by the API; no Google Drive dependency needed. | Fixed in v1.0.2 |
| "`pytest tests/ -v` → 9 passing" | True then, 15 now. Both runners work (`pytest` and `--self-test`). | Verified |
| Missing from the roadmap's deploy list | `SHEETFLOW_ADMIN_TOKEN` (operator endpoints return 503 without it) and `SHEETFLOW_PUBLIC_BASE_URL` (otherwise PDF links are useless). | Added |
| "Dunning emails go to spam" | Moot before v1.0.2 — there were no emails. Now a real SPF/DKIM concern. | Open |

---

## 1. Phase 0 — what is actually left in the code

| # | Item | Why it matters | Est. |
|---|---|---|---|
| 1 | ~~Pydantic v2~~ | Done | — |
| 2 | ~~Code.gs missing functions~~ | Done, with admin auth | — |
| 3 | ~~Launcher / duplicate engine~~ | Done | — |
| 4 | ~~Email dispatch~~ | Done, DRY_RUN default | — |
| 5 | ~~PDF link instead of path~~ | Done (signed URL) | — |
| 6 | ~~Apps Script retry~~ | Done: 3 attempts, 1s/2s/4s | — |
| 7 | ~~Failed-email retry~~ | Done: retried on the next daily run and via `--retry-failed`, capped at 3 attempts, without ever double-sending. | — |
| 8 | ~~Digest delivery~~ | Done: `--run-digest` emails each owner; `SHEETFLOW_SCHEDULER_ENABLED=true` runs it Mondays with no cron. | — |
| 9 | ~~Payment gateway adapter~~ | Done: Razorpay webhook verifies the native signature and marks `PAID`; hosted links when keys are set. | — |
| 10 | ~~Job intake~~ | Done: official Freelancer API + RSS, scored, deduped, human-approved. No bidding. | — |

**Contrast with the roadmap's Phase 0 (4 h):** the security and correctness work here was
larger, but every item in this table is now genuinely done.

---

## 2. Phase 1 — first client (₹3,000–5,000 setup, ₹1,000–1,500/mo)

### Deploy (server)
```bash
pip install -r requirements.txt
export SHEETFLOW_DB_PATH=/srv/sheetflow/clients.db
export SHEETFLOW_PDF_DIR=/srv/sheetflow/invoices
export SHEETFLOW_PUBLIC_BASE_URL=https://billing.yourdomain.com   # PDF links
export SHEETFLOW_ADMIN_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
export DRY_RUN=true                     # flip to false only after a real send test
export SMTP_HOST=smtp.gmail.com SMTP_PORT=587 SMTP_USER=you@gmail.com SMTP_PASSWORD=<app-password>
python sheetflow_engine.py --serve --host 0.0.0.0 --port 8000
```
Put Caddy/nginx in front for HTTPS. On Render/Railway, pin the PDF dir to a persistent disk —
the filesystem is ephemeral and PDF links are served from disk.

### Onboard the client
```bash
python sheetflow_engine.py --create-tenant \
  --db /srv/sheetflow/clients.db --tenant-id tnt_client01 \
  --business-name "Client One" --home-state Maharashtra --tax-id 27AABCS1429B1Z1 \
  --sheet-id <google_sheet_id> --upi-vpa client1@okaxis \
  --public-base-url https://billing.yourdomain.com
```
Paste the printed `SHEETFLOW_CONFIG` into `Code.gs`, add the admin token, create the
`Invoices_Ledger` tab with the 16 columns from `DATA_MODEL.md`.

### Acceptance test (do this with the client watching)
1. One row, `Status=READY_TO_BILL`, run **Process All READY_TO_BILL Rows**.
2. Column I = server total, M = payment link, N = **openable PDF URL**, L = `SENT`.
3. Open the N link in a private window → PDF downloads; delete `?token` → `401`.
4. Run **Trigger Due & Overdue Reminders** → alert lists reminder count and subjects.
5. Run **Send Weekly Cash-Flow Digest** → alert shows billed/collected/outstanding.
6. Confirm the digest email actually arrived at the client's owner address (`--owner-email`).

From then on the server can run it: set `SHEETFLOW_SCHEDULER_ENABLED=true`, or cron
`--run-daily` at 08:00 and `--run-digest` on Mondays. Nobody needs to open the sheet.

### What to charge for
Setup ₹3,000–5,000 (sheet setup, tenant, GSTIN/state, UPI VPA, one training call).
Retainer ₹1,000–1,500 for digest review, reminder monitoring, and invoice-format tweaks.

---

## 3. Phase 2 — clients 2 and 3 (₹3,000–4,500/mo recurring)

`--create-tenant` is the whole onboarding step; each client is one tenant row (own state,
secret, UPI VPA, sheet id). One Apps Script project per client sheet.

| Retainer deliverable | Automatable today? |
|---|---|
| Weekly cash-flow digest | Pull via menu / admin GET; **email needs a cron** (Phase 0 item 8) |
| Overdue/stuck invoice check | Yes — dunning run reports `dispatched_count` and failures |
| Failed webhook / PDF triage | Manual (log inspection) |
| New invoice format tweaks | Manual |

**Operational limits at 3 clients:** one SQLite file on one box is fine at this volume
(WAL enabled, `busy_timeout=5000`). Watch disk for PDFs; no retention policy exists yet.

**Pricing math unchanged:** 2 local ≈ ₹2,000–3,000/mo, 3 local ≈ ₹3,000–4,500/mo.

---

## 4. Phase 3 — international (month 4–6)

Currency handling is ready (`currency_code`, `currency_symbol()`, per-tenant PDF symbol).
Everything else is not:

| Need | Status |
|---|---|
| VAT/Sales-tax logic | Absent. Only India GST (CGST/SGST/IGST) is modeled. Run `tax_rate_pct=0` or it will label foreign tax as IGST. |
| Card checkout (Stripe) | Absent. The fake `pay.sheetflow.in` link is gone; the only rail is the tenant's UPI deep link, which is INR-only. |
| Real gateway signature verification | Absent (see Phase 0 item 9). |
| Email deliverability (SPF/DKIM) | Required once `DRY_RUN=false`. |
| WhatsApp reminders | **Not implemented, and not planned here.** |

---

## 5. Risks (corrected)

| Risk | Reality / mitigation |
|---|---|
| "Emails go to spam" | Only once email is enabled. Warm the domain, set SPF/DKIM, send from a real mailbox. |
| PDF renderer missing on server | Automatic ReportLab fallback; `/health` shows `pdf_engine`. Verify after deploy. |
| Client's sheet columns differ | Provide the sheet as part of setup; `Code.gs` indexes columns positionally. |
| Webhook secret leaks via a shared sheet | Rotate with `--create-tenant` (without `--webhook-secret`) and update `Code.gs`. |
| Operator endpoints exposed | `SHEETFLOW_ADMIN_TOKEN` unset ⇒ `503`, so a fresh deploy fails closed. |
| Lost reminders after an SMTP outage | Known: `FAILED` is surfaced, not retried (Phase 0 item 7). |

---

## 6. Revenue projection (unchanged)

| Month | Clients | Setup | Recurring | Total |
|---|---|---|---|---|
| 1 | 1 | ₹3,000–5,000 | ₹0 | ₹3,000–5,000 |
| 2 | 2 | ₹3,000–5,000 | ₹1,000–1,500 | ₹4,000–6,500 |
| 3 | 3 | ₹3,000–5,000 | ₹2,000–3,000 | ₹5,000–8,000 |
| 4–6 | 3 local + 2–3 intl | varies | ₹3,000–4,500 + $300–900 | ₹6,000–15,000+ |

---

## 7. Do not build yet

React dashboard (Sheets is the UI), double-entry bookkeeping, Tally/QuickBooks export,
multi-currency tax engines, WhatsApp. None of them are on the path to the first ₹5,000.
