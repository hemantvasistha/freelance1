# SheetFlow Engine

A spreadsheet row becomes a GST-correct PDF invoice with a tap-to-pay UPI link, and the
payment chases itself. One Python service plus one Google Apps Script file.

**Read [OFFER.md](OFFER.md) first** — it is how this becomes ₹1,000–4,000/month.

## What it actually does

1. A row's status is set to `READY_TO_BILL` → the server computes GST
   (CGST+SGST within the home state, IGST across states) and renders an A4 PDF.
2. It writes back to the sheet: total (col I), payment link (M), signed PDF URL (N),
   status (L).
3. Reminders fire at T-3, due date, and T+7, with catch-up windows if a run is missed.
   A reminder is never sent twice for the same stage.
4. The owner gets a Monday cash-flow digest by email.
5. A payment marks the invoice `PAID` — via the UPI link being confirmed by the operator,
   a SheetFlow-signed webhook, or a native Razorpay webhook.
6. Optional job intake scores Freelancer.com projects against what this can deliver and
   keeps a shortlist for you to approve.

## Quickstart

```bash
cd sheetflow_project
py -m venv .venv                      # Windows; python3 -m venv on macOS/Linux
.venv/Scripts/python.exe -m pip install -r requirements.txt
cp .env.example .env                  # then set SHEETFLOW_PUBLIC_BASE_URL (+ SMTP if sending)

.venv/Scripts/python.exe -m pytest tests -q     # 38 tests
python sheetflow_engine.py --serve --port 8000  # from the repo root
```

Python 3.11+ (verified on 3.14.7). PDFs use WeasyPrint when its native libs exist and
fall back to ReportLab automatically; `GET /health` reports which engine is live.

There is **no demo data**: a fresh install has an empty ledger.

## Commands

| Command | What it does |
|---|---|
| `--serve` | Run the API (uvicorn) |
| `--self-test` | Run the full test suite |
| `--create-tenant …` | Onboard a client, print the paste-ready `Code.gs` config |
| `--list-tenants` | List onboarded clients |
| `--run-daily` | Dispatch reminders for every tenant (cron target) |
| `--run-digest` | Email the weekly digest (cron target) |
| `--retry-failed` | Resend reminders whose delivery previously failed |
| `--jobs-scan` / `--jobs-list` / `--jobs-approve ID` | Compliant job intake |
| `--as-of YYYY-MM-DD` | Override the run date for `--run-daily`/`--run-digest` |

### Onboarding a client

```bash
python sheetflow_engine.py --create-tenant \
  --tenant-id tnt_client01 --business-name "Sunrise Dental" \
  --home-state Maharashtra --tax-id 27AABCS1429B1Z1 \
  --sheet-id <google_sheet_id> --upi-vpa clinic@okaxis \
  --owner-email owner@clinic.in \
  --public-base-url https://billing.example.com
```

Paste the printed `SHEETFLOW_CONFIG` into the client's `Code.gs`, add the admin token,
create the `Invoices_Ledger` tab, then run the sheet menu → **Process All READY_TO_BILL Rows**.
Re-running the command rotates the webhook secret unless you pass `--webhook-secret`,
which silently breaks any sheet still holding the old value.

## Environment

| Variable | Why it matters |
|---|---|
| `SHEETFLOW_DB_PATH` | SQLite ledger. One file per deployment. |
| `SHEETFLOW_PDF_DIR` | Must be a **persistent** disk; PDF links are served from here. |
| `SHEETFLOW_PUBLIC_BASE_URL` | Without it, column N gets a local path nobody can open. |
| `SHEETFLOW_ADMIN_TOKEN` | Server-side admin endpoints return 503 when unset (fails closed). Customers use their own per-tenant operator token instead; the admin token never goes into a client script. |
| `DRY_RUN` | `true` records emails instead of sending them. |
| `SMTP_*` | Required for live email when `DRY_RUN=false`. |
| `SHEETFLOW_OWNER_EMAIL` | Digest recipient when a tenant has no `owner_email`. |
| `SHEETFLOW_SCHEDULER_ENABLED` | In-process daily + Monday digest, no cron needed. |
| `RAZORPAY_KEY_ID` / `_KEY_SECRET` | Enables card payment links instead of UPI-only. |
| `RAZORPAY_WEBHOOK_SECRET` | Required for automatic `PAID` from Razorpay. |
| `SHEETFLOW_JOB_FEEDS` | Comma-separated RSS URLs for job intake. |
| `SHEETFLOW_FREELANCER_QUERIES` | Search terms for the official Freelancer API. |

## How money reaches you

- **UPI (default, zero fee):** column M holds a `upi://pay?…` link; the money lands in the
  client's own VPA. This needs no gateway, no KYC, no fee.
- **Razorpay (optional):** with keys set, each invoice gets a hosted payment page, and the
  webhook marks it `PAID`. Point the dashboard webhook at
  `POST /api/v1/webhooks/razorpay` with the secret from `RAZORPAY_WEBHOOK_SECRET`.
- **Marketplace work:** the platform pays out to your bank; nothing here is involved.

## Production deploy

A single-VPS runbook (systemd unit, Caddy HTTPS, nightly backup, cron) is in
[`deploy/README.md`](../deploy/README.md).

## Unattended runs

Either enable the in-process scheduler:

```bash
SHEETFLOW_SCHEDULER_ENABLED=true SHEETFLOW_SCHEDULER_HOUR=8 python sheetflow_engine.py --serve
```

or use cron (more robust on a VPS):

```cron
0 8 * * *   cd /srv/sheetflow && python sheetflow_engine.py --run-daily
30 8 * * 1  cd /srv/sheetflow && python sheetflow_engine.py --run-digest
```

## Job intake (compliant by construction)

`--jobs-scan` reads only official, machine-readable sources (Freelancer.com's public
projects API and any RSS you add). It never logs in, never scrapes HTML, never bids.
Each lead is scored for **fit with this exact product**, budget, and freshness, then
waits for `--jobs-approve`. Details and the scoring rules are in `src/jobs/intake.py`.

## Limits worth knowing

- One SQLite file, one process. Fine for a handful of clients; move to Postgres before ~20.
- A `FAILED` email is retried on the next daily run (max 3 attempts) and on demand with
  `--retry-failed`; it never re-sends a reminder that already went out.
- WhatsApp is not implemented. Don't promise it.
- PDFs are stored on local disk with no retention policy; back up the directory.
