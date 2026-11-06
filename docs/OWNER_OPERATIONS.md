# Owner operations — the routine, and what to do when it breaks

Target: **under 30 minutes a week** once the deployment is stable, excluding customer
conversations. This is an estimate from the number of moving parts, not a measurement.

## Weekly routine (10 minutes)

```bash
curl -s https://billing.yourdomain.com/health
curl -s -H "X-SheetFlow-Admin-Token: $SHEETFLOW_ADMIN_TOKEN" \
     https://billing.yourdomain.com/api/v1/admin/ops
```

Read `needs_attention` first, then the `exceptions` block:

| Field | Meaning | Action |
|---|---|---|
| `sheet_updates_not_yet_pulled` | Payment/overdue changes the customer's sheet has not acknowledged | Usually resolves within 6h via the trigger. If it grows for one tenant, their triggers are missing — tell them to run `installTriggers`. |
| `failed_reminder_deliveries` | Reminder emails SMTP rejected | Check `SMTP_PASSWORD`/app password. Retries happen automatically on the next daily run, up to 3 attempts. |
| `failed_invoice_emails` | Invoice email failed | Same cause. The invoice still exists and the sheet still has its links; resend manually if needed. |

Then confirm the daily backup exists (below).

## Backup and restore

```bash
# Back up the database safely while the service is running
sqlite3 /srv/sheetflow/clients.db ".backup '/srv/sheetflow/backups/clients-$(date +%F).db'"

# Back up the PDFs alongside it
tar -czf /srv/sheetflow/backups/invoices-$(date +%F).tar.gz -C /srv/sheetflow invoices

# Copy both off the box — a backup on the same disk is not a backup
rclone copy /srv/sheetflow/backups remote:sheetflow-backups
```

Restore drill (do this once a month, into a scratch path):

```bash
sqlite3 /tmp/restore-test.db ".restore '/srv/sheetflow/backups/clients-2026-11-06.db'"
sqlite3 /tmp/restore-test.db "SELECT COUNT(*) FROM invoices;"
```

If that count is not plausible, your backups are broken — fix it before you need them.

## Deploying an update

```bash
cd /srv/sheetflow
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart sheetflow
curl -s https://billing.yourdomain.com/health
```

Migrations run automatically on start (`init_db` is additive only: new columns and
tables, never drops). Take a backup before a deploy that touches `src/database.py`.

Rollback: `git checkout <previous-tag-or-sha>` then restart. Data added by the newer
build remains; additive columns are ignored by older code, which is why migrations are
additive.

## Exception playbook

**Reminder went to the wrong person.** Check `client_email` in the row; the scheduler
reads it from the invoice, not the sheet. Fix the row's email and re-ingest.

**A payment is confirmed but the sheet still says SENT.** The outbox row is pending.
Have the customer run menu item 4, or wait for the 6-hour trigger. If the row's
`Invoice_No` was renamed in the sheet, the sync cannot match it — restore the original
number; the queue keeps the change until it matches.

**An invoice was double-billed.** `idempotency_key = sha256(tenant:invoice_number:issue_date)`.
Same number and issue date returns the existing invoice. Two different issue dates are
genuinely two invoices.

**SMTP is dead and reminders keep failing.** Set `DRY_RUN=true` and restart. Reminders
are then recorded instead of sent, so nothing is lost while you fix credentials.

**A customer leaves.** Export their sheet, then delete their tenant row and PDFs. There
is no self-service deletion; it is a manual SQL operation, so do it deliberately and
record the date.

## Credentials and rotation

- `SHEETFLOW_ADMIN_TOKEN`: owner-only, used by the commands above and cron. Rotate by
  setting a new value in `.env`, restarting, and updating any script that uses it.
- Tenant `OPERATOR_TOKEN`: rotate by re-onboarding that tenant; update their script.
- Razorpay keys and SMTP password: live in `/srv/sheetflow/.env` (chmod 600). Never in
  this repository, never in a customer's sheet.

## What still needs a human

Onboarding (judgement about their sheet), payment confirmation for UPI without a
gateway, and support conversations. Everything else — invoicing, PDFs, reminders,
digests, ledger write-back, retries — is automated.
