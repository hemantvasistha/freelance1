# Deploy SheetFlow on one VPS

Target: a ₹300–600/month Linux box (1 vCPU, 1 GB RAM), Ubuntu 24.04. Everything below is
for a **single** deployment serving a handful of clients; it is not multi-region.

## 1. Base setup

```bash
sudo adduser --system --group --home /srv/sheetflow sheetflow
sudo -u sheetflow -H bash
cd /srv/sheetflow
python3 -m venv .venv
.venv/bin/pip install -r /path/to/sheetflow_project/requirements.txt
```

Copy `sheetflow_project/` and `sheetflow_engine.py` into `/srv/sheetflow` so the layout is:

```
/srv/sheetflow/
├── sheetflow_engine.py
├── sheetflow_project/
├── .venv/
├── .env              # chmod 600, never committed
├── clients.db        # created on first start
├── invoices/         # SHEETFLOW_PDF_DIR
└── backups/
```

## 2. Environment

```bash
cp sheetflow_project/.env.example /srv/sheetflow/.env   # then edit
```

Minimum for a real client:

```ini
SHEETFLOW_DB_PATH=/srv/sheetflow/clients.db
SHEETFLOW_PDF_DIR=/srv/sheetflow/invoices
SHEETFLOW_PUBLIC_BASE_URL=https://billing.example.com   # must match Caddy
SHEETFLOW_ADMIN_TOKEN=<openssl rand -base64 32>   # server-side only; never give this to a client
DRY_RUN=false
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASSWORD=<app password>
SHEETFLOW_OWNER_EMAIL=you@gmail.com
```

`/health` must report `public_base_url: configured`, `scheduler: enabled|off`, and the real
`email_delivery` mode. If `DRY_RUN` is still `true`, reminders are recorded, not sent.

## 3. Service + HTTPS

```bash
sudo cp deploy/sheetflow.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sheetflow
sudo systemctl status sheetflow --no-pager

sudo cp deploy/Caddyfile /etc/caddy/Caddyfile   # replace billing.example.com
sudo systemctl reload caddy
curl -s https://billing.example.com/health
```

The app binds `127.0.0.1:8000` only; Caddy terminates TLS and adds HSTS automatically.

## 4. Unattended runs

Pick **one**:

```bash
# (a) in-process — simplest, nothing else to install
sudo sed -i 's/^SHEETFLOW_SCHEDULER_ENABLED=.*/SHEETFLOW_SCHEDULER_ENABLED=true/' /srv/sheetflow/.env
sudo systemctl restart sheetflow
```

```cron
# (b) cron — survives a wedged process, easier to see in logs
0 8 * * *   cd /srv/sheetflow && .venv/bin/python sheetflow_engine.py --run-daily  >> backups/cron.log 2>&1
30 8 * * 1  cd /srv/sheetflow && .venv/bin/python sheetflow_engine.py --run-digest >> backups/cron.log 2>&1
```

Run the daily job once by hand and read the JSON: `dispatched_count` should be > 0 on a day
something is due, and `errors` must be empty.

## 5. Backups

```bash
chmod +x deploy/sheetflow-backup.sh
sudo cp deploy/sheetflow-backup.sh /srv/sheetflow/
# 02:30 UTC nightly
30 2 * * * root SHEETFLOW_DB_PATH=/srv/sheetflow/clients.db SHEETFLOW_PDF_DIR=/srv/sheetflow/invoices /srv/sheetflow/sheetflow-backup.sh >> /var/log/sheetflow-backup.log 2>&1
```

Copy `backups/` off the box (rclone to Drive, or `scp` weekly). A backup that lives only on
the server is not a backup.

## 6. First client

```bash
cd /srv/sheetflow
sudo -u sheetflow .venv/bin/python sheetflow_engine.py --create-tenant \
  --tenant-id tnt_client01 --business-name "Sunrise Dental" \
  --home-state Maharashtra --tax-id 27AABCS1429B1Z1 \
  --sheet-id <sheet id> --upi-vpa clinic@okaxis \
  --owner-email owner@clinic.in \
  --public-base-url https://billing.example.com
```

Paste the printed config into the client's Apps Script, add the admin token, run
**Process All READY_TO_BILL Rows**, then verify the four things in `OFFER.md` §5 in front of
the client.

## 7. Verify, don't assume

```bash
curl -s https://billing.example.com/health
# an empty body is rejected by request validation before auth runs
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://billing.example.com/api/v1/webhooks/sheet-row-ready \
  -H "Content-Type: application/json" -d '{}'          # expect 422
# a well-formed row with a bad signature must be refused
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://billing.example.com/api/v1/webhooks/sheet-row-ready \
  -H "Content-Type: application/json" -H "X-SheetFlow-Signature: $(printf '0%.0s' {1..64})" \
  -d '{"tenant_id":"x","sheet_row_number":2,"invoice_number":"INV-1","client_name":"c","client_email":"c@example.com","client_state":"MH","item_description":"d","subtotal_major":"100.00","tax_rate_pct":"18.00","issue_date":"2026-11-06","due_date":"2026-11-13","status":"READY_TO_BILL"}'   # expect 401
# operator endpoint without a token must be closed
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://billing.example.com/api/v1/dunning/run \
  -H "Content-Type: application/json" -d '{"tenant_id":"x","as_of_date":"2026-01-01"}'   # expect 401
# a real invoice PDF link must open, and must fail without its token
```

Better than curls by hand, run the whole list at once:

```bash
py scripts/verify_deployment.py --base-url https://billing.example.com \
  --admin-token "$SHEETFLOW_ADMIN_TOKEN" --operator-token "$OPERATOR_TOKEN" \
  --webhook-secret "$WEBHOOK_SECRET" --tenant tnt_client01
```

It prints each check as PASS / BLOCKED / FAIL and exits non-zero on any FAIL. `BLOCKED`
means a credential is genuinely missing (live email, Razorpay) - not that the check passed.

## Known limits at this size

- One SQLite file and one process. Fine for a few clients; move to Postgres before ~20.
- PDFs are plain files: back up the directory, and never point it at ephemeral storage.
- No inbound webhook queue: if the box is down when a sheet posts, Apps Script retries three
  times (1s/2s/4s) and then that row is simply not billed until someone re-runs the menu.
