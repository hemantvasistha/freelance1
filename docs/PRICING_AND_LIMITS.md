# Pricing, limits and unit economics

These are **hypotheses to test with real customers**, not validated market facts. The
engineering repository cannot establish willingness to pay.

## The offer

| Item | Price | What it includes |
|---|---|---|
| Setup (one-time) | ₹2,000 | Sheet wired to the ledger, GSTIN/state/UPI configured, triggers installed, first invoice produced live |
| Monthly | ₹999 | One business, unlimited invoices, PDF + payment link, automated reminders, Monday summary, email support |

No free tier. The trial is the demo itself: one real invoice is produced and delivered
in front of the customer before they pay. That costs ~20 minutes and produces no
standing support obligation.

## Revenue needed

| Customers | MRR | Covers fixed costs? |
|---|---|---|
| 1 | ₹999 | Yes (≈₹620 fixed) |
| 3 | ₹2,997 | Yes, with ~₹2,300 gross margin |
| 5 | ₹4,995 | Meets the ₹1,000–₹5,000 target |

Gross margin per customer is high; the real cost is **your time**: ~45 min onboarding
plus support. At ₹999/month, a customer who needs 30 minutes of support every month is
worth roughly ₹500/hour effective — acceptable, but not for a talkative customer.

## Enforced limits vs stated limits — read this honestly

The software does **not** currently enforce plan limits. There is one plan, no invoice
counter, no subscription billing, and no per-tenant invoice cap. Limits in this document
are a commercial contract, not a technical restriction.

Deliberately not built yet: Razorpay subscription billing, usage metering, plan
downgrades. With 1–5 customers, invoicing them manually or collecting by UPI is cheaper
and has fewer failure modes than a billing integration that itself needs reconciliation.

## What is technically enforced

| Behaviour | Enforced? |
|---|---|
| Tenant isolation (one customer cannot read another's invoices) | **Yes**, per-tenant operator token + HMAC |
| Payment must be signed and in the right currency, never underpaid | **Yes** |
| Duplicate invoice on re-ingest | **Yes**, idempotency key |
| One gateway payment link per invoice | **Yes**, reused from the stored reference |
| Invoice count / user count / plan caps | **No** |
| Rate limiting per tenant | **Yes**, 60 operator requests/minute |

## Costs (assumptions)

| Item | Monthly |
|---|---|
| VPS 1 vCPU / 1 GB | ₹300–600 |
| Domain | ~₹70 |
| Email (low-volume SMTP) | ₹0–200 |
| Razorpay fees | 2% + GST per collected payment (only if used) |
| **Fixed total** | **≈₹620** |

Break-even: one customer. Profit at five customers: ≈₹4,375 before payment fees and
your time.

## Terms to publish with the offer

- Billed monthly in advance; cancel by email, effective at the end of the paid month.
- No refund for a partial month; setup fee is non-refundable once the sheet is wired.
- Price may change for new customers after the pilot; existing customers keep their
  rate for at least 3 months after a change is announced.
- Data lives in the customer's own Google Sheet; the server holds invoices, PDFs and an
  audit log, exportable on request.
- Service is provided as-is for invoicing assistance. It is not tax advice and does not
  file returns.

## What to validate before changing any of this

1. Will a coaching centre pay ₹2,000 setup after seeing one invoice?
2. Does ₹999 feel cheaper than their time spent chasing, or does it feel like a
   subscription they will cancel in month three?
3. Which single feature would they miss if it disappeared — reminders, the PDF, or the
   payment link? That answer decides where the next engineering hour goes.
