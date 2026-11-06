#!/usr/bin/env python3
"""SheetFlow Engine launcher (`sheetflow_engine.py`).

This is a thin entrypoint over the `sheetflow_project` package, so there is exactly
one copy of the application code:

    python sheetflow_engine.py --self-test   # run the full unit + HTTP test suite
    python sheetflow_engine.py --serve       # run the FastAPI server (uvicorn)
    python sheetflow_engine.py --create-tenant --tenant-id tnt_x \
        --business-name "Acme Traders" --home-state Maharashtra --sheet-id 1AbC...
    python sheetflow_engine.py --list-tenants
    python sheetflow_engine.py --jobs-scan     # official feeds only; a human approves each lead

Setup (once):
    py -m venv sheetflow_project/.venv
    sheetflow_project/.venv/Scripts/python.exe -m pip install -r sheetflow_project/requirements.txt
"""
import argparse
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent / "sheetflow_project"
if not (PROJECT_ROOT / "src").is_dir():
    sys.exit(f"Could not find the SheetFlow package at {PROJECT_ROOT}")

# The package imports itself as `src.*`, so the package root must be on sys.path.
sys.path.insert(0, str(PROJECT_ROOT))

import json  # noqa: E402

from src.database import get_db_connection  # noqa: E402
from src.jobs import list_leads, run_scan, set_lead_status  # noqa: E402
from src.jobs.store import ensure_schema  # noqa: E402
from src.main import app  # noqa: E402
from src.onboarding import list_tenants, onboard_tenant  # noqa: E402


def _print_leads(leads: list[dict]) -> None:
    if not leads:
        print("No leads match that filter.")
        return
    for lead in leads:
        budget = ""
        low_minor = lead.get("budget_min_minor") or 0
        high_minor = lead.get("budget_max_minor") or 0
        if low_minor or high_minor:
            low, high = low_minor / 100, high_minor / 100
            if high and high != low:
                budget = f"{lead.get('currency') or '?'} {low:g}-{high:g}"
            else:
                budget = f"{lead.get('currency') or '?'} from {low:g}"
        print(f"[{lead['score']:>2}] {(lead.get('title') or '')[:78]}")
        print(f"     {lead.get('capability') or 'no fit'}" + (f" | {budget}" if budget else ""))
        print(f"     {lead.get('url') or ''}")


def run_self_test() -> int:
    suite = unittest.TestLoader().discover(str(PROJECT_ROOT / "tests"), top_level_dir=str(PROJECT_ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="SheetFlow Engine — unified CLI")
    parser.add_argument("--self-test", action="store_true", help="Run all unit and HTTP integration tests")
    parser.add_argument("--serve", action="store_true", help="Run the FastAPI server with uvicorn")
    parser.add_argument("--create-tenant", action="store_true", help="Onboard a client tenant and print its Apps Script config")
    parser.add_argument("--list-tenants", action="store_true", help="List onboarded tenants")
    parser.add_argument("--run-daily", action="store_true", help="Dispatch reminders for every tenant (cron target)")
    parser.add_argument("--run-digest", action="store_true", help="Email the weekly cash-flow digest (cron target)")
    parser.add_argument("--retry-failed", action="store_true", help="Resend reminders whose delivery previously failed")
    parser.add_argument("--as-of", help="Override the run date (YYYY-MM-DD) for --run-daily/--run-digest")
    parser.add_argument("--owner-email", help="Owner email for the digest (--create-tenant)")
    parser.add_argument("--jobs-scan", action="store_true", help="Fetch, score and store job leads from official feeds")
    parser.add_argument("--jobs-list", action="store_true", help="Show stored job leads")
    parser.add_argument("--jobs-status", default="NEW", help="Filter for --jobs-list (NEW/APPROVED/SKIPPED/all)")
    parser.add_argument("--jobs-min-score", type=int, default=0, help="Minimum score for --jobs-list")
    parser.add_argument("--jobs-approve", metavar="LEAD_ID", help="Mark a lead approved for a manual pitch")
    parser.add_argument("--jobs-skip", metavar="LEAD_ID", help="Mark a lead skipped")
    parser.add_argument("--db", default="sheetflow.db", help="SQLite DB file path")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host for --serve")
    parser.add_argument("--port", type=int, default=8000, help="Bind port for --serve")
    parser.add_argument("--tenant-id", help="Tenant id for --create-tenant")
    parser.add_argument("--business-name", help="Business name for --create-tenant")
    parser.add_argument("--home-state", help="GST home state for --create-tenant")
    parser.add_argument("--sheet-id", help="Google Sheet id for --create-tenant")
    parser.add_argument("--tax-id", help="GSTIN for --create-tenant")
    parser.add_argument("--upi-vpa", help="UPI VPA for --create-tenant")
    parser.add_argument("--currency", default="INR", help="Currency code for --create-tenant")
    parser.add_argument("--webhook-secret", help="Reuse an existing webhook secret instead of rotating it")
    parser.add_argument("--public-base-url", default="", help="Public HTTPS base URL used in invoice PDF links")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()
    if args.list_tenants:
        print(json.dumps(list_tenants(args.db), indent=2))
        return 0
    if args.create_tenant:
        missing = [
            flag
            for flag, value in (
                ("--tenant-id", args.tenant_id),
                ("--business-name", args.business_name),
                ("--home-state", args.home_state),
                ("--sheet-id", args.sheet_id),
            )
            if not value
        ]
        if missing:
            parser.error("--create-tenant requires " + ", ".join(missing))
        result = onboard_tenant(
            args.db,
            tenant_id=args.tenant_id,
            business_name=args.business_name,
            home_state=args.home_state,
            google_sheet_id=args.sheet_id,
            tax_id=args.tax_id,
            upi_vpa=args.upi_vpa,
            owner_email=args.owner_email,
            currency_code=args.currency,
            webhook_secret=args.webhook_secret,
            public_base_url=args.public_base_url,
        )
        print(json.dumps(result, indent=2))
        return 0
    if args.retry_failed:
        from src.dunning.runner import retry_failed

        report = retry_failed(args.db)
        print(json.dumps(report, indent=2))
        return 0
    if args.run_daily or args.run_digest:
        from src.dunning.runner import run_daily, run_weekly_digest

        report = (
            run_daily(args.db, as_of_date=args.as_of)
            if args.run_daily
            else run_weekly_digest(args.db, as_of_date=args.as_of)
        )
        print(json.dumps(report, indent=2))
        return 1 if report.get("errors") else 0
    if args.jobs_scan:
        print(json.dumps(run_scan(args.db), indent=2))
        return 0
    if args.jobs_list:
        status = None if args.jobs_status.lower() == "all" else args.jobs_status.upper()
        with get_db_connection(args.db) as conn:
            ensure_schema(conn)
            _print_leads(list_leads(conn, status=status, min_score=args.jobs_min_score))
        return 0
    if args.jobs_approve or args.jobs_skip:
        target = args.jobs_approve or args.jobs_skip
        new_status = "APPROVED" if args.jobs_approve else "SKIPPED"
        with get_db_connection(args.db) as conn:
            ensure_schema(conn)
            ok = set_lead_status(conn, target, new_status)
        print(f"{target}: {new_status}" if ok else f"{target}: not found")
        return 0 if ok else 1
    if args.serve:
        import uvicorn

        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
