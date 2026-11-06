# Backup and recovery

The entire product state is one SQLite file plus the generated PDFs. That makes backup
cheap, and it makes losing the file catastrophic. Treat the database file as the asset.

## What must be backed up

| Item | Path | Recoverable if lost? |
|---|---|---|
| Database (tenants, invoices, payments, audit trail, sheet outbox) | `SHEETFLOW_DB_PATH` (default `sheetflow.db`) | No — this is the business record |
| Generated PDFs | `SHEETFLOW_PDF_DIR` | Partly — invoice rows survive, files must be regenerated |
| `.env` / secrets | not in git | No — keep in a password manager |
| Code | git remote | Yes |

## Procedure (hot backup, service running)

SQLite's online backup API is safe against a live writer and produces a consistent
snapshot, unlike `cp` of a file in WAL mode.

```bash
sqlite3 /var/lib/sheetflow/sheetflow.db ".backup '/var/backups/sheetflow-$(date +%F-%H%M).db'"
```

Cron (daily 02:15, keep 14 days, copy off the machine):

```cron
15 2 * * * sqlite3 /var/lib/sheetflow/sheetflow.db ".backup '/var/backups/sheetflow-$(date +\%F).db'" && find /var/backups -name 'sheetflow-*.db' -mtime +14 -delete && rclone copy /var/backups remote:sheetflow-backups
```

Without an off-machine copy this is not a backup: a provider disk failure takes the
backups with it.

## Restore procedure

```bash
systemctl stop sheetflow
cp /var/backups/sheetflow-2026-11-06.db /var/lib/sheetflow/sheetflow.db
chown sheetflow:sheetflow /var/lib/sheetflow/sheetflow.db
systemctl start sheetflow
curl -fsS https://billing.example.com/health
```

Then check business-level integrity, not just that the process started:

```bash
sqlite3 /var/lib/sheetflow/sheetflow.db "SELECT COUNT(*) FROM invoices; SELECT COUNT(*) FROM payments; SELECT COALESCE(SUM(amount_minor),0) FROM payments;"
```

## Executed restore drill

`scripts/backup_restore_drill.py` builds a database with a tenant, five invoices, one
manual payment and the resulting audit rows, takes a hot backup, restores it into a
separate file, and compares row counts and financial totals.

Result on 2026-11-06 (`match: true`):

| Check | Before | Restored |
|---|---|---|
| tenants | 1 | 1 |
| invoices | 5 | 5 |
| payments | 1 | 1 |
| audit_events | 10 | 10 |
| sheet_sync_outbox | 0 | 0 |
| invoice total (minor) | 2,065,000 | 2,065,000 |
| payment total (minor) | 413,000 | 413,000 |

This proves the procedure copies data correctly in this environment. It does **not** prove
a restore on the production host, from the off-machine copy, or under load — that drill is
still required at deployment (`docs/implementation/PROJECT_STATUS.md`, blocker 4).

## Objectives

- **RPO:** up to 24 hours of invoice/payment history lost if the host dies between
  backups. Acceptable for a business issuing tens of invoices a month; move to hourly if a
  customer's volume makes a day of history material.
- **RTO:** under 15 minutes with the steps above (stop, copy, start, verify), assuming the
  code and `.env` are present.

## Known gaps

1. No automated backup-failure alerting yet (cron output is not watched).
2. No restore verification on a schedule — the drill is manual and currently unautomated.
3. PDFs are not included in the backup command above; they are regenerable from invoice
   rows, but the regeneration path is not scripted.
