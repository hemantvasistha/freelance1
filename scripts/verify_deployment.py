"""Post-deployment verification for a running SheetFlow instance.

Run this against the real HTTPS origin after every deployment. It exercises the
customer path (signed sheet row -> invoice -> PDF -> write-back), the authorization
boundaries, and reports BLOCKED where a credential is genuinely missing instead of
pretending the check passed.

    py scripts/verify_deployment.py \
      --base-url https://billing.example.com \
      --admin-token "$SHEETFLOW_ADMIN_TOKEN" \
      --operator-token "$TENANT_OPERATOR_TOKEN" \
      --webhook-secret "$TENANT_WEBHOOK_SECRET" \
      --tenant tnt_client01

Exit code 0 only when every check passes (BLOCKED checks do not fail the run but are
printed loudly).
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import urllib.error
import urllib.request

ADMIN_HEADER = "X-SheetFlow-Admin-Token"
OPERATOR_HEADER = "X-SheetFlow-Operator-Token"
SIGNATURE_HEADER = "X-SheetFlow-Signature"


def call(method: str, url: str, body: bytes | None = None, headers: dict | None = None):
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)
    except Exception as exc:  # noqa: BLE001 - reported as a transport failure
        return 0, str(exc).encode(), {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a deployed SheetFlow instance")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--admin-token", required=True)
    parser.add_argument("--operator-token", required=True)
    parser.add_argument("--webhook-secret", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--invoice-number", default=None)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    results: list[dict] = []

    def record(name: str, state: str, detail: str) -> None:
        results.append({"check": name, "state": state, "detail": detail})
        print(f"[{state:7}] {name}: {detail}")

    # 1. HTTPS and health -----------------------------------------------------
    status, body, _ = call("GET", f"{base}/health")
    if status != 200:
        record("https_health", "FAIL", f"GET /health -> {status} ({body[:120]!r})")
    else:
        health = json.loads(body)
        detail = (
            f"status={health.get('status')} email={health.get('email_delivery')} "
            f"pdf={health.get('pdf_engine')} base_url={health.get('public_base_url')} "
            f"scheduler={health.get('scheduler')}"
        )
        if not base.startswith("https://"):
            record("https_health", "BLOCKED", f"reached over plain HTTP; TLS not exercised. {detail}")
        else:
            record("https_health", "PASS", detail)
        if health.get("public_base_url") != "configured":
            record("public_base_url", "FAIL", "SHEETFLOW_PUBLIC_BASE_URL is unset: PDF links are unusable")

    # 2. Landing page ---------------------------------------------------------
    status, body, headers = call("GET", f"{base}/")
    text = body.decode("utf-8", "replace")
    if status == 200 and "999" in text:
        placeholder = "example.com" in text or "__SUPPORT_EMAIL__" in text
        record(
            "landing_page",
            "FAIL" if placeholder else "PASS",
            "served 200 text/html with pricing"
            + ("; BUT contains an example.com contact placeholder" if placeholder else ""),
        )
    else:
        record("landing_page", "FAIL", f"GET / -> {status}")

    # 3. One well-formed, signed row payload, reused by the auth checks below
    invoice_number = args.invoice_number or "INV-VERIFY-0001"
    row = {
        "tenant_id": args.tenant,
        "sheet_row_number": 2,
        "invoice_number": invoice_number,
        "client_name": "Verification Client",
        "client_email": "client@example.com",
        "client_state": "Maharashtra",
        "item_description": "Monthly retainer",
        "subtotal_major": "3500.00",
        "tax_rate_pct": "18.00",
        "issue_date": "2026-11-06",
        "due_date": "2026-11-13",
        "status": "READY_TO_BILL",
    }
    raw = json.dumps(row, separators=(",", ":")).encode()
    signature = hmac.new(args.webhook_secret.encode(), raw, hashlib.sha256).hexdigest()

    # 4. Authorization boundaries --------------------------------------------
    status, _, _ = call(
        "POST", f"{base}/api/v1/webhooks/sheet-row-ready", raw,
        {"Content-Type": "application/json"},
    )
    record("unsigned_webhook_rejected", "PASS" if status == 401 else "FAIL", f"expected 401, got {status}")

    status, _, _ = call(
        "POST", f"{base}/api/v1/webhooks/sheet-row-ready", raw,
        {"Content-Type": "application/json", SIGNATURE_HEADER: "0" * 64},
    )
    record("forged_signature_rejected", "PASS" if status == 401 else "FAIL", f"expected 401, got {status}")

    status, _, _ = call(
        "POST", f"{base}/api/v1/webhooks/sheet-row-ready", b"{}",
        {"Content-Type": "application/json"},
    )
    record(
        "malformed_body_rejected", "PASS" if status == 422 else "FAIL",
        f"expected 422 from schema validation, got {status}",
    )

    status, _, _ = call("GET", f"{base}/api/v1/operator/sheet-updates")
    record(
        "operator_endpoint_needs_token", "PASS" if status == 401 else "FAIL",
        f"expected 401, got {status}",
    )

    status, _, _ = call(
        "POST", f"{base}/api/v1/admin/manual-payment", b"{}",
        {"Content-Type": "application/json"},
    )
    record(
        "manual_payment_needs_admin_token", "PASS" if status in (401, 403) else "FAIL",
        f"expected 401/403, got {status}",
    )

    # 5. Customer path: signed row -> invoice -> PDF -------------------------
    status, body, _ = call(
        "POST", f"{base}/api/v1/webhooks/sheet-row-ready", raw,
        {"Content-Type": "application/json", SIGNATURE_HEADER: signature},
    )
    invoice = json.loads(body) if status == 200 else {}
    if status != 200:
        record("signed_row_creates_invoice", "FAIL", f"POST signed row -> {status} ({body[:160]!r})")
    else:
        record(
            "signed_row_creates_invoice", "PASS",
            f"{invoice.get('invoice_number')} total={invoice.get('total_major')} "
            f"created={invoice.get('created_new')} delivery={invoice.get('delivery_status')}",
        )
        if invoice.get("delivery_status") in ("FAILED", "SKIPPED"):
            record(
                "invoice_email_delivered", "BLOCKED",
                f"server reported delivery={invoice.get('delivery_status')} "
                f"({(invoice.get('delivery_error') or 'no SMTP configured')[:120]})",
            )

    # Replay of the identical body must not create a second invoice.
    status, body, _ = call(
        "POST", f"{base}/api/v1/webhooks/sheet-row-ready", raw,
        {"Content-Type": "application/json", SIGNATURE_HEADER: signature},
    )
    replay = json.loads(body) if status == 200 else {}
    record(
        "row_replay_is_idempotent",
        "PASS" if status == 200 and replay.get("created_new") is False else "FAIL",
        f"created_new={replay.get('created_new')} on replay",
    )

    pdf_url = invoice.get("pdf_url") or ""
    if pdf_url:
        path = pdf_url.split("?", 1)[0]
        url = path if path.startswith("http") else f"{base}{path}"
        status, _, _ = call("GET", url)
        record("pdf_denied_without_token", "PASS" if status == 401 else "FAIL", f"expected 401, got {status}")
        status, pdf_body, _ = call("GET", pdf_url)
        ok = status == 200 and pdf_body.startswith(b"%PDF")
        record(
            "pdf_served_with_token", "PASS" if ok else "FAIL",
            f"expected 200 application/pdf, got {status}, {len(pdf_body)} bytes, "
            f"magic={pdf_body[:5]!r}",
        )
    else:
        record("pdf_served_with_token", "FAIL", "invoice response contained no pdf_url")

    # 5. Money: manual UPI reconciliation -> write-back -> ack ----------------
    status, body, _ = call(
        "POST", f"{base}/api/v1/admin/manual-payment",
        json.dumps(
            {
                "tenant_id": args.tenant,
                "invoice_number": invoice_number,
                "amount_major": str(invoice.get("total_major") or "4130.00"),
                "reference": "UPI/VERIFY-0001",
                "recorded_by": "verify_deployment.py",
            }
        ).encode(),
        {"Content-Type": "application/json", ADMIN_HEADER: args.admin_token},
    )
    payment = json.loads(body) if status == 200 else {}
    record(
        "manual_payment_reconciles",
        "PASS" if status == 200 and payment.get("status") == "PAID" else "FAIL",
        f"status={payment.get('status')} became_paid={payment.get('became_paid')} "
        f"outstanding={payment.get('outstanding_minor')}",
    )

    status, body, _ = call("GET", f"{base}/api/v1/operator/sheet-updates", headers={OPERATOR_HEADER: args.operator_token})
    updates = json.loads(body) if status == 200 else {}
    pending = updates.get("updates", [])
    record(
        "write_back_queued", "PASS" if status == 200 and pending else "FAIL",
        f"count={updates.get('count')} statuses={[u.get('status') for u in pending]}",
    )
    if pending:
        status, body, _ = call(
            "POST", f"{base}/api/v1/operator/sheet-updates/ack",
            json.dumps({"ids": [u["id"] for u in pending]}).encode(),
            {"Content-Type": "application/json", OPERATOR_HEADER: args.operator_token},
        )
        ack = json.loads(body) if status == 200 else {}
        record(
            "write_back_acknowledged", "PASS" if ack.get("acknowledged") == len(pending) else "FAIL",
            f"acknowledged={ack.get('acknowledged')} of {len(pending)}",
        )

    status, body, _ = call("GET", f"{base}/api/v1/admin/ops", headers={ADMIN_HEADER: args.admin_token})
    ops = json.loads(body) if status == 200 else {}
    record(
        "owner_ops_view", "PASS" if status == 200 and ops.get("invoices_by_status") else "FAIL",
        f"status={status} by_status={json.dumps(ops.get('invoices_by_status'))[:160]}",
    )

    # 6. Razorpay webhook (needs a configured secret to be meaningful) --------
    status, body, _ = call(
        "POST", f"{base}/api/v1/webhooks/razorpay", b'{"event":"payment.captured"}',
        {"Content-Type": "application/json", "X-Razorpay-Signature": "deadbeef"},
    )
    if status == 503:
        record(
            "razorpay_signature_check", "BLOCKED",
            "RAZORPAY_WEBHOOK_SECRET is not configured on this deployment",
        )
    else:
        record(
            "razorpay_signature_check", "PASS" if status == 401 else "FAIL",
            f"forged signature -> {status} (expected 401)",
        )

    failures = [r for r in results if r["state"] == "FAIL"]
    blocked = [r for r in results if r["state"] == "BLOCKED"]
    print()
    print(json.dumps({"base_url": base, "checks": results, "failures": len(failures), "blocked": len(blocked)}, indent=2))
    print(f"\n{len(results) - len(failures) - len(blocked)} passed, {len(blocked)} blocked, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
