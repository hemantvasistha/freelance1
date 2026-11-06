"""Local load harness for SheetFlow.

Honest about what it measures: an in-process FastAPI TestClient against a SQLite file
in a temporary directory, on this machine. It is useful for finding algorithmic and
query problems and for comparing changes over time. It is NOT a production capacity
measurement — there is no network, no TLS, no reverse proxy and no second process.

    py sheetflow_project/.venv/Scripts/python.exe scripts/load_test.py --tenants 4 --invoices-per-tenant 10 --reads 200
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1] / "sheetflow_project"
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from src.database import get_db_connection, init_db  # noqa: E402
from src.ingestion.sheet_sync import process_sheet_row_webhook  # noqa: E402
from src.main import app  # noqa: E402
from src.models import SheetFlowRepository  # noqa: E402
from src.schemas import SheetRowWebhookPayload  # noqa: E402


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(round((p / 100.0) * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def summarise(values: list[float]) -> dict:
    return {
        "count": len(values),
        "p50_ms": round(percentile(values, 50), 1),
        "p95_ms": round(percentile(values, 95), 1),
        "max_ms": round(max(values), 1) if values else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="SheetFlow local load harness")
    parser.add_argument("--tenants", type=int, default=4)
    parser.add_argument("--invoices-per-tenant", type=int, default=10)
    parser.add_argument("--reads", type=int, default=200)
    args = parser.parse_args()

    tmp = tempfile.TemporaryDirectory()
    db_path = os.path.join(tmp.name, "load.db")
    pdf_dir = os.path.join(tmp.name, "pdfs")
    os.environ["SHEETFLOW_DB_PATH"] = db_path
    os.environ["SHEETFLOW_PDF_DIR"] = pdf_dir
    os.environ["SHEETFLOW_PUBLIC_BASE_URL"] = "https://billing.example.com"
    os.environ["SHEETFLOW_ADMIN_TOKEN"] = "load-token"
    os.environ["DRY_RUN"] = "true"
    os.environ.pop("SHEETFLOW_ENV", None)
    init_db(db_path)

    ingest_ms: list[float] = []
    invoices = 0
    with get_db_connection(db_path) as conn:
        repo = SheetFlowRepository(conn)
        for t in range(args.tenants):
            tenant_id = f"tnt_load_{t:02d}"
            repo.upsert_tenant(
                tenant_id=tenant_id,
                business_name=f"Load Co {t}",
                home_state="Haryana",
                google_sheet_id=f"sheet_load_{t}",
                webhook_secret=f"sec_load_{t}",
                upi_vpa=f"load{t}@okaxis",
                owner_email=f"owner{t}@example.com",
            )
            for i in range(args.invoices_per_tenant):
                payload = SheetRowWebhookPayload(
                    tenant_id=tenant_id,
                    sheet_row_number=i + 2,
                    invoice_number=f"INV-{t}-{i:04d}",
                    client_name="Load Client",
                    client_email="client@example.com",
                    client_state="Haryana",
                    item_description="Monthly retainer",
                    subtotal_major="3500.00",
                    tax_rate_pct="18.00",
                    issue_date="2026-11-06",
                    due_date="2026-11-13",
                    status="READY_TO_BILL",
                )
                started = time.perf_counter()
                process_sheet_row_webhook(conn, payload, pdf_dir, "https://billing.example.com")
                ingest_ms.append((time.perf_counter() - started) * 1000.0)
                invoices += 1

    client = TestClient(app)
    health_ms: list[float] = []
    ops_ms: list[float] = []
    for _ in range(args.reads):
        started = time.perf_counter()
        probe = client.get("/health")
        health_ms.append((time.perf_counter() - started) * 1000.0)
        if probe.status_code != 200:
            print(f"health check failed: {probe.status_code}", file=sys.stderr)
            return 1

        started = time.perf_counter()
        ops = client.get(
            "/api/v1/admin/ops", headers={"X-SheetFlow-Admin-Token": "load-token"}
        )
        ops_ms.append((time.perf_counter() - started) * 1000.0)
        if ops.status_code != 200:
            print(f"ops check failed: {ops.status_code}", file=sys.stderr)
            return 1

    result = {
        "environment": {
            "harness": "fastapi TestClient (in-process, no network/TLS/reverse proxy)",
            "database": "SQLite file (WAL) in a temporary directory",
            "python": sys.version.split()[0],
        },
        "dataset": {"tenants": args.tenants, "invoices": invoices},
        "invoice_ingest_including_pdf": summarise(ingest_ms),
        "health_read": summarise(health_ms),
        "owner_ops_read_5_queries": summarise(ops_ms),
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
