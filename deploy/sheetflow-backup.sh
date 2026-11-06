#!/usr/bin/env bash
# Nightly backup for a live SheetFlow deployment.
#
# The database runs in WAL mode, so `cp` can capture a torn file. `sqlite3 .backup`
# takes a consistent snapshot while the API keeps writing. Keeps 14 days.
set -euo pipefail

DB="${SHEETFLOW_DB_PATH:-/srv/sheetflow/sheetflow.db}"
PDF_DIR="${SHEETFLOW_PDF_DIR:-/srv/sheetflow/generated_invoices}"
DEST="${SHEETFLOW_BACKUP_DIR:-/srv/sheetflow/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "$DEST"

command -v sqlite3 >/dev/null 2>&1 || {
	echo "sqlite3 CLI is required: apt install sqlite3" >&2
	exit 1
}

sqlite3 "$DB" ".backup '$DEST/ledger-$STAMP.db'"
echo "ledger snapshot: $DEST/ledger-$STAMP.db"

if [ -d "$PDF_DIR" ]; then
	tar -czf "$DEST/invoices-$STAMP.tar.gz" -C "$(dirname "$PDF_DIR")" "$(basename "$PDF_DIR")"
	echo "invoice archive: $DEST/invoices-$STAMP.tar.gz"
fi

find "$DEST" -type f -mtime +14 -delete
echo "pruned backups older than 14 days"
