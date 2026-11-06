# SheetFlow

One spreadsheet row becomes a GST-correct invoice PDF with a tap-to-pay UPI or card link,
gets emailed to the customer, and then chases itself until it is paid — writing the result
back into the same row.

One Python service (FastAPI + SQLite) and one Google Apps Script file. No dashboard for the
customer to learn: their sheet is the interface.

## Layout

```
sheetflow_engine.py      CLI + server entrypoint
app/                     the application package (src/) and its tests
  src/                   FastAPI app, billing, dunning, reconciliation, sheets
  tests/                 66 tests, run with --self-test
apps_script/Code.gs      the paste-into-Google-Sheets client
site/index.html          landing page, served at /
deploy/                  systemd unit, Caddyfile, backup script, VPS runbook
docs/                    operations, onboarding, limits, product specs
scripts/                 deploy verification, load harnesses, restore drill
requirements.txt         runtime dependencies
.env.example             every setting, with safe defaults
```

## Quickstart

```bash
py -m venv app/.venv                                    # Windows
app/.venv/Scripts/python.exe -m pip install -r requirements.txt
cp .env.example .env                                    # then set SHEETFLOW_PUBLIC_BASE_URL

app/.venv/Scripts/python.exe sheetflow_engine.py --self-test      # 66 tests
app/.venv/Scripts/python.exe sheetflow_engine.py --serve --port 8000
```

On macOS/Linux use `python3 -m venv app/.venv` and `app/.venv/bin/python`.

A fresh install has an empty ledger. There is no demo data and no seeded customer.

## Onboard one client

```bash
app/.venv/Scripts/python.exe sheetflow_engine.py --create-tenant \
  --tenant-id tnt_client01 --business-name "Sunrise Dental" \
  --home-state Maharashtra --tax-id 27AABCS1429B1Z1 \
  --sheet-id <google_sheet_id> --upi-vpa clinic@okaxis \
  --owner-email owner@clinic.in --public-base-url https://billing.example.com
```

Paste the printed config into that client's `apps_script/Code.gs`, create the
`Invoices_Ledger` tab, then run **Process All READY_TO_BILL Rows** from the sheet menu.
Each tenant gets its own operator token; the server-wide admin token never goes into a
customer's script.

## Deploy

Single-VPS runbook (systemd, Caddy HTTPS, nightly backup, cron):
[deploy/README.md](deploy/README.md). Then prove it:

```bash
py scripts/verify_deployment.py --base-url https://your-domain \
  --admin-token "$SHEETFLOW_ADMIN_TOKEN" --operator-token "$OPERATOR_TOKEN" \
  --webhook-secret "$WEBHOOK_SECRET" --tenant tnt_client01
```

It prints PASS / BLOCKED / FAIL for every customer-path and authorization check and exits
non-zero on any failure.

## Documentation

| Document | What it covers |
|---|---|
| [docs/CUSTOMER_ONBOARDING.md](docs/CUSTOMER_ONBOARDING.md) | Onboarding a business, step by step |
| [docs/OWNER_OPERATIONS.md](docs/OWNER_OPERATIONS.md) | Day-to-day running, cron, troubleshooting |
| [docs/BACKUP_AND_RECOVERY.md](docs/BACKUP_AND_RECOVERY.md) | Backup procedure and an executed restore drill |
| [docs/LOAD_TEST_REPORT.md](docs/LOAD_TEST_REPORT.md) | Measured latency, the PDF bottleneck, stages not run |
| [docs/PRICING_AND_LIMITS.md](docs/PRICING_AND_LIMITS.md) | Price, what is and is not enforced |
| [docs/implementation/PROJECT_STATUS.md](docs/implementation/PROJECT_STATUS.md) | Per-feature verification matrix |
| [docs/product/OFFER.md](docs/product/OFFER.md) | The commercial offer and pitch |
| [AGENT_STATE.md](AGENT_STATE.md) | Session handoff: what is done, blocked, next |

## Honest status

Verified locally by tests: invoicing, GST split, PDF + signed links, payment reconciliation
(including manual UPI settlement), reminders, sheet write-back, tenant isolation,
concurrency safety, backup/restore.

Not yet verified against live providers: SMTP delivery, Razorpay API calls, Apps Script
running inside a real Google Sheet, and HTTPS termination. Those need credentials and a
host; they are listed as blockers, not claimed as working.
