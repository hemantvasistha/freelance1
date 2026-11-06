# PROJECT_CONTEXT.md — Persistent AI Memory & State Anchor

> **CRITICAL INSTRUCTION FOR AI CODING ASSISTANTS (Cursor / Claude Code / Windsurf / Gemini):**  
> Read this file FIRST before proposing or writing any code. After completing any task or module, you MUST update Section 4 (Current Build State) and log the change in `CHANGELOG.md` and `project_manifest.json` so the next session never loses context.

## 1. Project Identity & Core Philosophy
- **Project Name:** SheetFlow (`sheetflow-engine`)
- **Builder Profile:** 1st-Year B.Tech Student building a cash-flowing B2B automation productized service (Target: ₹1,000–₹4,000/month immediate recurring baseline, scaling to $1,000–$4,000/month via international retainers and micro-SaaS licensing over 4 years).
- **Architectural Rule ("Own the Plumbing"):** Small businesses hate learning new dashboards ([Sage Invoice Automation Report](https://www.sage.com/en-ca/blog/invoice-automation-boosting-efficiency/)). Therefore, **Google Sheets is the Frontend UI**. Python (FastAPI) + SQLite + Google Apps Script is the invisible backend engine.

## 2. Locked Tech Stack (Do Not Change Without Explicit Approval)
| Layer | Technology | Rationale |
| :--- | :--- | :--- |
| **Client UI / Control Plane** | Google Sheets + Google Apps Script (`Code.gs`) | Zero UI learning curve for SME owners; instant visual status tracking |
| **Backend API & Webhooks** | Python 3.11+, FastAPI, Pydantic v2 | Fast webhook handling, strict schema validation, easy deployment |
| **Database / State Store** | SQLite (`sheetflow.db`) with SQLAlchemy 2.0 | Single-file portability, zero cloud DB bill for initial clients |
| **PDF Rendering** | Jinja2 HTML Templates + WeasyPrint / ReportLab | Clean, deterministic A4 invoice generation with embedded UPI QR / payment links |
| **Payment & Messaging** | Razorpay / Stripe Webhooks + Gmail SMTP / WhatsApp API | Automated reconciliation and multi-channel dunning reminders |

## 3. Directory Structure
```text
sheetflow-engine/
├── PRD.md                      # Product specification & market grounding
├── PROJECT_CONTEXT.md          # THIS FILE: Persistent AI state & architecture rules
├── BUILD_PROMPT.md             # Copy-paste system prompt for AI coding sessions
├── DATA_MODEL.md               # Database & Google Sheets schema definitions
├── project_manifest.json       # Machine-readable module status tracker
├── CHANGELOG.md                # Chronological log of completed milestones
├── README.md                   # Operator manual: setup, commands, env, limits
├── OFFER.md                    # The thing you sell: price, pitch, objections, delivery
├── requirements.txt            # Pinned dependency set (FastAPI, Pydantic v2, WeasyPrint + ReportLab, httpx)
├── .env.example                # Environment template (DB path, PDF dir, admin token, DRY_RUN, SMTP)
├── .gitignore                  # Keeps .venv, *.db ledgers and .env out of version control
├── apps_script/
│   └── Code.gs                 # Google Sheets custom menu & webhook trigger script
└── src/
    ├── config.py               # Environment variables & Pydantic settings
    ├── database.py             # SQLite connection & session management
    ├── models.py               # SQLAlchemy ORM tables matching DATA_MODEL.md
    ├── schemas.py              # Pydantic request/response schemas + GST math + currency_symbol()
    ├── onboarding.py           # Creates client tenants & prints the Code.gs config block
    ├── ingestion/
    │   └── sheet_sync.py       # Reads Google Sheet rows & validates tax math
    ├── billing/
    │   ├── pdf_generator.py    # Renders HTML template to A4 PDF (WeasyPrint → ReportLab fallback)
    │   ├── document_links.py   # Signed, client-openable invoice PDF URLs
    │   └── payment_links.py    # Generates Razorpay/Stripe payment links + UPI QR
    ├── notifications/
    │   └── mailer.py           # Outbound email (RecordingMailer for DRY_RUN, SmtpMailer live)
    ├── dunning/
    │   ├── scheduler.py        # Evaluates due dates (T-3, T+0, T+7) & dispatches reminders
    │   └── runner.py           # Unattended daily run + weekly digest across all tenants
    ├── jobs/
    │   ├── intake.py           # Official Freelancer API + RSS parsing and capability scoring
    │   └── store.py            # job_leads storage, dedupe, human approval status
    └── reconciliation/
        ├── webhooks.py         # SheetFlow HMAC payment verification & reconciliation
        └── razorpay.py         # Native Razorpay webhook + hosted payment links
```

## 4. Current Build State (Single Source of Truth)
- **Current Phase:** `REVENUE_READY` (38/38 tests passing on Python 3.14.7 / FastAPI 0.162 / Pydantic 2.14)
- **Next Immediate Milestone:** `M5_FIRST_PAYING_CLIENT` (deploy, onboard one real business, collect the setup fee). `OFFER.md` is the script.
- **Environment (verified 2026-11-06):** `py -m venv .venv` then `.venv/Scripts/python.exe -m pip install -r requirements.txt`. Run `python sheetflow_engine.py --self-test` / `--serve` / `--create-tenant`. There is no demo or seed data: a fresh install starts with an empty ledger.
- **Known gaps still open (do not claim these as done):** WhatsApp is not implemented (email is); no backend → Google Sheets write-back (the Apps Script owns sheet writes); single SQLite file, no multi-process locking story; job intake only reads official feeds, it never bids.
- **Earning path:** the product is sold as invoicing automation to local businesses (₹2–3k setup + ₹1–1.5k/month). Everything else — job intake, Razorpay, scheduler — exists to support that or to find more of it, not as a separate business.
- **Completed Modules:**
  - [x] `M0`: Architecture, PRD, Data Model & AI Context Persistence Pack (`PRD.md`, `DATA_MODEL.md`, `project_manifest.json`)
  - [x] `M1`: Core SQLite schema, idempotent repository & integer minor-unit GST split calculator (`src/config.py`, `src/database.py`, `src/models.py`, `src/schemas.py`)
  - [x] `M2`: Deterministic UPI URI + checkout link generator & A4 HTML/PDF invoice renderer (`src/billing/payment_links.py`, `src/billing/pdf_generator.py`)
  - [x] `M3`: Google Sheets Apps Script bridge & row ingestion webhook (`Code.gs`, `src/ingestion/sheet_sync.py`)
  - [x] `M4`: 3-Stage automated dunning scheduler (`T-3`, `T+0`, `T+7`), weekly executive digest & HMAC-SHA256 payment webhook reconciliation (`src/dunning/scheduler.py`, `src/reconciliation/webhooks.py`)
  - [x] Unified single-file production runner (`sheetflow_engine.py`) & verified sample output (`Sample_Tax_Invoice_INV-2026-014.pdf`)
  - [x] Repair pass (v1.0.1): Pydantic v2 validators, ReportLab PDF fallback, signed sheet webhooks, admin-token guarded operator endpoints, ingestion status lock-down, dunning catch-up windows, atomic audit inserts, WAL SQLite. `sheetflow_engine.py` is now a thin launcher over `src/` (the old duplicated copy is gone).
  - [x] Revenue-path pass (v1.0.2): real email delivery with DRY_RUN default (`src/notifications/mailer.py`), signed client-openable invoice PDF URLs (`GET /api/v1/invoices/.../pdf`), `--create-tenant` / `--list-tenants` onboarding CLI, Apps Script retry with backoff. Test suite grew from 9 to 15 tests.
  - [x] Purge (v1.0.3): removed the fake `pay.sheetflow.in` checkout, the demo pipeline and its hardcoded tenant/secret, the stale `sheetflow.db`, and 12 root-level duplicate docs. Payment link is now the tenant's real UPI deep link.
  - [x] Unattended + payments + intake (v1.1.0): `src/dunning/runner.py` (daily run, weekly digest, in-process scheduler), `tenants.owner_email` with additive migration, `src/reconciliation/razorpay.py` (signed webhook → `PAID`, hosted payment links when keys exist), `src/jobs/` (official API/RSS intake, scoring, dedupe, human approval), `OFFER.md`. Test suite: 36.
  - [x] Retry pass (v1.1.1): `retry_failed_reminders` + `--retry-failed`; the audit claim is updated in place, attempts capped at 3, and retries run before the daily dispatch so a fresh failure never burns an attempt. Repository initialised as a git repo with CI (38 tests).

## 5. Coding Conventions & Guardrails
1. **Monetary Precision:** Never use IEEE 754 floats (`float`) for currency calculations. Always store amounts in **minor units (paise/cents as `INTEGER`)** or `Decimal(12, 2)`.
2. **Idempotency First:** Every invoice generation and reminder dispatch must check `idempotency_key` in `audit_events` before sending an email or creating a payment link.
3. **Dry-Run Mode:** Always support `DRY_RUN=true` in `.env` so testing never emails real clients or charges live cards.
4. **End-of-Session Protocol:** Before ending any coding turn, update Section 4 of this file, flip the module status in `project_manifest.json`, and append a dated entry to `CHANGELOG.md`.
