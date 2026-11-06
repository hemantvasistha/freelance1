"""Client onboarding: create or refresh a tenant and emit the Apps Script config.

Onboarding a client used to mean writing a Python snippet against
`SheetFlowRepository.upsert_tenant`. This module makes it a repeatable operation with
a printable checklist, so client #2 and #3 take minutes instead of a code edit.
"""
import json
import secrets
from dataclasses import asdict
from typing import Any

from src.database import get_db_connection, init_db
from src.models import SheetFlowRepository


def render_apps_script_config(
    tenant_id: str, webhook_secret: str, public_base_url: str
) -> str:
    """The `SHEETFLOW_CONFIG` block to paste at the top of `Code.gs`."""
    base = (public_base_url or "https://your-sheetflow-domain.example.com").rstrip("/")
    return (
        "const SHEETFLOW_CONFIG = {\n"
        f'  API_BASE_URL: "{base}",\n'
        f'  TENANT_ID: "{tenant_id}",\n'
        f'  WEBHOOK_SECRET: "{webhook_secret}",\n'
        '  ADMIN_TOKEN: "paste-the-server-SHEETFLOW_ADMIN_TOKEN-here",\n'
        '  LEDGER_TAB_NAME: "Invoices_Ledger"\n'
        "};"
    )


def onboard_tenant(
    db_path: str,
    *,
    tenant_id: str,
    business_name: str,
    home_state: str,
    google_sheet_id: str,
    tax_id: str | None = None,
    upi_vpa: str | None = None,
    currency_code: str = "INR",
    owner_email: str | None = None,
    webhook_secret: str | None = None,
    public_base_url: str = "",
) -> dict[str, Any]:
    """Creates or updates a tenant and returns everything needed to wire up a sheet.

    If `webhook_secret` is omitted a fresh one is generated — which invalidates any
    sheet still holding the old value, so the response says whether it rotated.
    """
    init_db(db_path)
    with get_db_connection(db_path) as conn:
        repo = SheetFlowRepository(conn)
        existing = repo.get_tenant(tenant_id)
        secret = webhook_secret or secrets.token_urlsafe(24)
        tenant = repo.upsert_tenant(
            tenant_id=tenant_id,
            business_name=business_name,
            home_state=home_state,
            google_sheet_id=google_sheet_id,
            webhook_secret=secret,
            tax_id=tax_id,
            currency_code=currency_code,
            upi_vpa=upi_vpa,
            owner_email=owner_email,
        )
        tenant_data = asdict(tenant)

    secret_rotated = existing is not None and existing.webhook_secret != secret
    return {
        "tenant": tenant_data,
        "webhook_secret": secret,
        "secret_rotated": secret_rotated,
        "created": existing is None,
        "apps_script_config": render_apps_script_config(tenant_id, secret, public_base_url),
        "next_steps": [
            "Paste apps_script_config into the client's Code.gs SHEETFLOW_CONFIG block.",
            "Set SHEETFLOW_PUBLIC_BASE_URL and SHEETFLOW_ADMIN_TOKEN on the server.",
            "Create the Invoices_Ledger tab with the 16 columns from DATA_MODEL.md.",
            "Add one READY_TO_BILL row and run 'Process All READY_TO_BILL Rows'.",
        ],
    }


def list_tenants(db_path: str) -> list[dict[str, Any]]:
    init_db(db_path)
    with get_db_connection(db_path) as conn:
        tenants = SheetFlowRepository(conn).list_tenants()
        return [
            {
                "tenant_id": t.id,
                "business_name": t.business_name,
                "home_state": t.home_state,
                "currency_code": t.currency_code,
                "is_active": t.is_active,
                "has_upi": bool(t.upi_vpa),
            }
            for t in tenants
        ]


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI shim
    import argparse

    parser = argparse.ArgumentParser(description="SheetFlow tenant onboarding")
    parser.add_argument("--db", default="sheetflow.db")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--business-name", required=True)
    parser.add_argument("--home-state", required=True)
    parser.add_argument("--sheet-id", required=True)
    parser.add_argument("--tax-id")
    parser.add_argument("--upi-vpa")
    parser.add_argument("--currency", default="INR")
    parser.add_argument("--webhook-secret")
    parser.add_argument("--public-base-url", default="")
    args = parser.parse_args(argv)

    result = onboard_tenant(
        args.db,
        tenant_id=args.tenant_id,
        business_name=args.business_name,
        home_state=args.home_state,
        google_sheet_id=args.sheet_id,
        tax_id=args.tax_id,
        upi_vpa=args.upi_vpa,
        currency_code=args.currency,
        webhook_secret=args.webhook_secret,
        public_base_url=args.public_base_url,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
