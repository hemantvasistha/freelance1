# Product Requirements Document (PRD): SheetFlow Invoice & Reconciliation Engine

## 1. Executive Summary & Strategic Thesis
**Product Name:** SheetFlow (`sheetflow-engine`)  
**Category:** B2B Financial Plumbing / Spreadsheet-to-Invoice & Payment Chaser Automation  
**Target Market:** Micro-agencies, wholesale distributors, clinics, coaching businesses, and local B2B service firms (5–50 employees) running billing out of messy spreadsheets.  
**Core Value Proposition:** Eliminates manual invoice drafting, GST/tax calculation errors, and awkward late-payment follow-ups by turning existing Google Sheets or Excel/CSV ledgers into an automated invoicing, PDF generation, WhatsApp/Email dispatch, and webhook reconciliation pipeline.

### Market Grounding & Why This Pays
- **Verified Administrative Bottleneck:** Implementing automated invoicing cuts processing costs by 60–80% and saves an average of 8.5 hours per month spent on manual billing tasks ([Winvesta Invoicing Automation Guide](https://www.winvesta.in/blog/freelancers/how-to-set-up-automated-invoicing-for-freelancers-a-simple-guide-that-saves-hours)).
- **Cash-Flow Recovery:** Late payments stem primarily from buried emails and forgotten due dates rather than refusal to pay; automated reminders and direct payment links remove collection friction ([Sage Invoice Automation Report](https://www.sage.com/en-ca/blog/invoice-automation-boosting-efficiency/)).
- **Freelance & Productized Service Pricing:** General Google Sheets freelancers charge a median of $13/hr ($10–$35/hr band) ([Upwork Google Sheets Cost Guide](https://www.upwork.com/hire/google-spreadsheet-freelancers/cost/)), while Python automation developers average $30/hr ($20–$45/hr for the 25th–75th percentile, with fixed-price automation builds clustering between $170 for micro-scripts and $600–$2,500 at the 75th–90th percentiles) ([Upwatcher Python Market Analysis](https://www.upwatcher.io/market/python/)). Specialists combining AI/workflow automation (n8n, Make, Python, Apps Script) command $35–$60/hr on freelance marketplaces ([Jahanzaib AI Automation Rate Study](https://www.jahanzaib.ai/blog/ai-automation-consultant-hourly-rate)).

## 2. User Personas & Pain Points
| Persona | Current Broken Workflow | Quantified Pain Point | SheetFlow Solution |
| :--- | :--- | :--- | :--- |
| **Local Agency / B2B Service Owner** | Copies rows from Google Sheets into Word/Canva, exports PDF, emails manually | 8.5+ hrs/month lost; 15–30% of invoices paid late ([Winvesta](https://www.winvesta.in/blog/freelancers/how-to-set-up-automated-invoicing-for-freelancers-a-simple-guide-that-saves-hours)) | 1-click or status-triggered PDF invoice + payment link + auto-chaser sequence |
| **Distributor / Trader (India / SME)** | Tracks orders in Excel/Sheets; calculates GST (CGST/SGST/IGST) manually | Calculation typos, missed follow-ups on 30-day credit terms ([Sage](https://www.sage.com/en-ca/blog/invoice-automation-boosting-efficiency/)) | Automated GST split validation, PDF generation, and Day -3 / Day 0 / Day +7 reminders |
| **Freelance Operator (Builder)** | Rebuilds scripts from scratch per client; AI coding assistant forgets context | High setup time eats into fixed-price margins ([Upwatcher](https://www.upwatcher.io/market/python/)) | Multi-tenant modular Python + Google Apps Script template with strict AI memory files |

## 3. Functional Scope & Feature Matrix (MVP v1.0)

### 3.1. Module A: Ledger Ingestion & Validation (`src/ingestion/`)
- **Google Sheets / CSV Connector:** Polls or receives webhook triggers when an order row status changes to `READY_TO_BILL`.
- **Idempotency Guard:** Generates a deterministic `idempotency_key` (`SHA256(client_id + invoice_number + issue_date)`) so duplicate triggers never double-bill a client.
- **Tax & Line-Item Validator:** Computes subtotal, discount, and tax splits (`CGST + SGST` for intra-state or `IGST` for inter-state in India; configurable VAT/Sales Tax for global clients) and flags schema errors in an `Audit_Log` sheet tab.

### 3.2. Module B: PDF Invoice & Payment Link Generator (`src/billing/`)
- **Branded HTML-to-PDF Engine:** Renders clean, print-ready A4 PDF invoices with dynamic company logos, GSTIN/Tax ID, itemized tables, and UPI QR code / Stripe / Razorpay payment links.
- **Payment Gateway Integration:** Generates unique payment links via Razorpay / Stripe API with `invoice_id` embedded in the metadata payload for zero-touch reconciliation.

### 3.3. Module C: Multi-Channel Dispatch & Dunning Engine (`src/dunning/`)
- **Automated Dispatch:** Sends the generated PDF invoice via SMTP/Gmail API and optional WhatsApp Business API template.
- **3-Stage Escalation Cadence:**
  - **T-3 Days (Pre-Due Courtesy):** Polite reminder with 1-click payment link.
  - **T+0 Days (Due Date Alert):** Due-today notification with invoice PDF attached.
  - **T+7 Days (Overdue Escalation):** Firm overdue notice; flags row red in Google Sheets and alerts the business owner on Telegram/Slack.

### 3.4. Module D: Webhook Reconciliation & Weekly Executive Digest (`src/reconciliation/`)
- **Instant Ledger Update:** Listens for `payment.captured` (Razorpay) or `checkout.session.completed` (Stripe) webhooks, marks the exact spreadsheet row as `PAID`, records `paid_at` timestamp and transaction reference, and halts all pending reminders.
- **Monday 8:00 AM Cash-Flow Digest:** Sends the business owner a weekly summary of Total Billed, Total Collected, Outstanding Receivables, and Top Overdue Accounts.

## 4. Non-Functional Requirements
- **Zero-Cost Hosting Tier for MVP:** Runs on Python 3.11+ (FastAPI + SQLite/PostgreSQL) deployable on free/low-cost tiers or directly inside the client's Google Workspace via Google Apps Script (`Code.gs`) for zero server overhead.
- **Auditability:** Every state transition (`DRAFT` -> `SENT` -> `VIEWED` -> `OVERDUE` -> `PAID`) is logged immutably in `audit_events`.
- **Context Continuity:** All architectural changes must update `PROJECT_CONTEXT.md`, `project_manifest.json`, and `CHANGELOG.md` before closing an AI coding session.

## 5. Out of Scope for v1.0
- Full double-entry bookkeeping or statutory tax filing (Tally/QuickBooks export CSV is supported instead).
- Custom React client portals (clients interact purely via email/WhatsApp PDF + payment link; business owners use their existing Google Sheet as the UI).
