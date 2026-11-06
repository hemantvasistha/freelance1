"""HTTP load harness against a running SheetFlow instance.

Unlike scripts/load_test.py (in-process), this drives real HTTP over TCP with concurrent
clients, signed webhook writes and signed-in reads.

    # terminal 1
    SHEETFLOW_DB_PATH=/tmp/load.db SHEETFLOW_ADMIN_TOKEN=t SHEETFLOW_PUBLIC_BASE_URL=https://x \
      py sheetflow_project/.venv/Scripts/python.exe sheetflow_engine.py --serve --port 8021

    # terminal 2
    py sheetflow_project/.venv/Scripts/python.exe scripts/http_load.py \
      --base-url http://127.0.0.1:8021 --token t --secret sec_load --users 25 --requests 400
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(round((p / 100.0) * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def read(url: str, token: str | None = None) -> int:
    headers = {"X-SheetFlow-Admin-Token": token} if token else {}
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:  # noqa: BLE001 - counted as a transport error
        return 0


def post_signed(url: str, secret: str, body: bytes) -> int:
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "X-SheetFlow-Signature": signature},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code
    except Exception:  # noqa: BLE001
        return 0


def timed(call) -> tuple[int, float]:
    started = time.perf_counter()
    status = call()
    return status, (time.perf_counter() - started) * 1000.0


def main() -> int:
    parser = argparse.ArgumentParser(description="SheetFlow HTTP load harness")
    parser.add_argument("--base-url", default="http://127.0.0.1:8021")
    parser.add_argument("--token", default="load-admin-token")
    parser.add_argument("--secret", default="sec_load", help="tenant webhook secret")
    parser.add_argument("--tenant", default="tnt_load_http")
    parser.add_argument("--users", type=int, default=25)
    parser.add_argument("--requests", type=int, default=400)
    parser.add_argument("--webhooks", type=int, default=40)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    started = time.perf_counter()

    read_latencies: list[float] = []
    read_errors = 0

    def one_read(index: int) -> tuple[int, float]:
        url = f"{base}/api/v1/admin/ops" if index % 2 else f"{base}/health"
        token = args.token if index % 2 else None
        return timed(lambda: read(url, token))

    with ThreadPoolExecutor(max_workers=args.users) as pool:
        for status, ms in pool.map(one_read, range(args.requests)):
            read_latencies.append(ms)
            if status != 200:
                read_errors += 1

    write_latencies: list[float] = []
    write_errors = 0

    def one_write(index: int) -> tuple[int, float]:
        payload = {
            "tenant_id": args.tenant,
            "sheet_row_number": index + 2,
            "invoice_number": f"INV-HTTP-{index:05d}",
            "client_name": "Load Client",
            "client_email": "client@example.com",
            "client_state": "Haryana",
            "item_description": "Monthly retainer",
            "subtotal_major": "3500.00",
            "tax_rate_pct": "18.00",
            "issue_date": "2026-11-06",
            "due_date": "2026-11-13",
            "status": "READY_TO_BILL",
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        url = f"{base}/api/v1/webhooks/sheet-row-ready"
        return timed(lambda: post_signed(url, args.secret, body))

    with ThreadPoolExecutor(max_workers=args.users) as pool:
        for status, ms in pool.map(one_write, range(args.webhooks)):
            write_latencies.append(ms)
            if status != 200:
                write_errors += 1

    elapsed = time.perf_counter() - started
    total = len(read_latencies) + len(write_latencies)
    errors = read_errors + write_errors

    report = {
        "target": base,
        "concurrency": args.users,
        "duration_s": round(elapsed, 2),
        "requests": total,
        "requests_per_second": round(total / elapsed, 1) if elapsed else 0,
        "error_rate_pct": round(100.0 * errors / total, 2) if total else 0,
        "reads": {
            "count": len(read_latencies),
            "p50_ms": round(percentile(read_latencies, 50), 1),
            "p95_ms": round(percentile(read_latencies, 95), 1),
            "p99_ms": round(percentile(read_latencies, 99), 1),
            "errors": read_errors,
        },
        "signed_webhook_writes": {
            "count": len(write_latencies),
            "p50_ms": round(percentile(write_latencies, 50), 1),
            "p95_ms": round(percentile(write_latencies, 95), 1),
            "p99_ms": round(percentile(write_latencies, 99), 1),
            "errors": write_errors,
        },
        "note": "Single uvicorn worker on localhost. No TLS, no reverse proxy, no CDN.",
    }
    print(json.dumps(report, indent=2))
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
