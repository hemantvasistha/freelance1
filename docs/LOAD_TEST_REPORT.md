# Load test report

Run 2026-11-06 on the development machine (Windows, Python 3.14.7, SQLite file in WAL
mode). The service was a single `uvicorn` worker on localhost with `DRY_RUN=true`, so no
email or payment provider was contacted and no PDF was served over the network.

Two harnesses were used:

- `scripts/load_test.py` — in-process, sequential; measures per-operation cost without
  network noise. Equivalent to Stage 1.
- `scripts/http_load.py` — real HTTP over TCP with concurrent clients, signed webhook
  writes and admin reads. Stages 2 and 3.

Every number below was printed by one of those scripts. Nothing is estimated.

## Stage 1 — baseline (sequential, in-process)

Dataset: 4 tenants, 40 invoices, 150 reads.

| Operation | p50 | p95 | max |
|---|---|---|---|
| Invoice ingest incl. PDF | 35.6 ms | 54.1 ms | 1389.2 ms |
| `GET /health` | 19.5 ms | 33.0 ms | 207.1 ms |
| `GET /api/v1/admin/ops` | 31.3 ms | 45.4 ms | 102.4 ms |

The 1.4 s maximum is first-invoice PDF engine warm-up, not steady state. Correctness at
this stage: all ingest calls produced exactly one invoice each; read calls all returned 200.

## Stage 2 — normal load (10 concurrent clients, real HTTP)

120 requests in 1.88 s → **63.8 req/s**, **0 % errors**.

| Class | p50 | p95 | p99 |
|---|---|---|---|
| Reads (`/health`, `/api/v1/admin/ops`) | 57.2 ms | 194.8 ms | 251.8 ms |
| Signed webhook writes (invoice ingest) | 433.9 ms | 615.8 ms | 658.5 ms |

## Stage 3 — expected load (25 concurrent clients, real HTTP)

340 requests in 3.60 s → **94.6 req/s**, **0 % errors**.

| Class | p50 | p95 | p99 |
|---|---|---|---|
| Reads | 131.8 ms | 182.9 ms | 429.4 ms |
| Signed webhook writes | 675.8 ms | 1080.5 ms | 1105.1 ms |

Server log after both stages: **0 tracebacks, 0 HTTP 500s**. Every write returned 200 and
every read returned 200, so the concurrency did not corrupt state or drop requests.

## Bottleneck found

**PDF rendering runs inside the invoice-ingest request.** Sequential ingest costs 36 ms
p50; under 25 concurrent clients the same work costs 675 ms p50 and 1.08 s p95, because
WeasyPrint/ReportLab is CPU-bound and the single worker serialises it. Reads are affected
indirectly (p50 rises from 57 ms to 132 ms) since they queue behind the same process.

This breaks the stated write SLO (p95 < 1 s) at 25 concurrent invoice writes. It is a
capacity limit of one worker, not a correctness defect: no request failed, no invoice was
duplicated, and the same rows were ingested once each.

Cheapest fix, in order: run the ingest PDF work in a small thread pool (WeasyPrint
releases the GIL partially), then move PDF generation to a background job with the sheet
row acknowledged as QUEUED, then add a second worker only after measuring the first two.
Not implemented yet — see `docs/implementation/EXECUTION_BACKLOG.md`.

## Stage 4–7 — NOT RUN

| Stage | Status | Why |
|---|---|---|
| 4 — peak (50 users) | NOT RUN | machine is also the workstation; numbers would be noise |
| 5 — stress to failure | NOT RUN | needs the peak stage first |
| 6 — soak (30–60 min) | NOT RUN | needs a staging host that is not this desktop |
| 7 — recovery (kill/restart, dependency outage) | PARTIAL | process restart verified manually; external-provider outage unverifiable without credentials |

Stage 6 is the one that matters most for a real deployment: it is the only way to detect
connection or memory growth over hours. It must be run on the deployment host before the
first paying customer, and this document must be updated with the result.

## What these numbers do and do not prove

They prove the request path is correct under 25 concurrent clients on one worker, that
errors stay at zero, and that SQLite WAL plus the existing indexes hold up at this size
(340 requests, 60 invoices written). They do **not** prove anything about a network with
TLS, a reverse proxy, a shared disk, or dozens of tenants with hundreds of thousands of
rows. Label them accordingly in any customer-facing material: they are not a capacity
guarantee.
