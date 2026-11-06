# SheetFlow — one-page offer & onboarding script

> The only thing you sell: **their messy invoice list becomes GST-correct PDF invoices
> with a tap-to-pay UPI link, and the reminders chase themselves.**
> Everything below is already built and tested in this repo.

---

## 1. Who buys this (pick 5 this week)

| Business | Why they say yes |
|---|---|
| Dental / physio clinics | Bill patients monthly, forget who hasn't paid |
| Coaching centres & tuition classes | Fees due on the 5th, chasing parents by WhatsApp |
| Wholesale / kirana distributors | 40–200 party invoices a month, all in Excel |
| CA / accounting practices | Client wants invoices + follow-up, they don't want to do it |
| Freelancers & small agencies | 5–15 retainer clients, chasing is awkward |

Door-to-door, WhatsApp, and the shop's own referral network beat cold email.

## 2. What you're selling

| Tier | What they get | Price |
|---|---|---|
| **Setup** | Sheet wired to their ledger, GSTIN + state + UPI configured, one training call | **₹2,000–3,000 one-time** |
| **Monthly** | Invoices keep generating, reminders fire automatically, Monday cash-flow digest | **₹1,000–1,500 / month** |
| **One-off job** | "Send me your Excel, I'll return the PDF invoices + payment links" | **₹500–1,500 per batch** |

₹4,000/month = **four clients at ₹1,000**, or **two setup jobs**. That is the whole target.

## 3. The 30-second pitch (say it exactly like this)

> "You send invoices in Excel and then chase people for payment, right?
> I set up a sheet where you paste the invoice row once — it creates a GST-correct PDF
> invoice with a UPI link your customer can tap, and it automatically reminds them
> 3 days before the due date and after. You get a Monday summary of how much is
> collected and how much is pending. Setup is ₹2,500, then ₹1,000 a month.
> First invoice today, so you can see it work before you pay."

## 4. Objections

| They say | You say |
|---|---|
| "I already use Tally / Zoho." | "Keep it. This handles the chasing part they don't do — the payment link and the reminders." |
| "My CA handles billing." | "Your CA enters the data. This makes the PDF and chases the payment. Your CA doesn't chase." |
| "Too expensive." | "One late payment of ₹10,000 costs more than a year of this." |
| "Is my data safe?" | "Your data stays in your Google Sheet. I don't touch your bank account; the money goes straight to your UPI ID." |
| "Let me think." | "Fair. Give me one invoice list — I'll do the first batch free so you can see the output." |

## 5. Delivery checklist (the part that must be boring)

```bash
# 1. Onboard the client (prints the Code.gs config + secret)
python sheetflow_engine.py --create-tenant \
  --tenant-id tnt_client01 --business-name "Sunrise Dental" \
  --home-state Maharashtra --tax-id 27AABCS1429B1Z1 \
  --sheet-id <their_sheet_id> --upi-vpa clinic@okaxis \
  --owner-email owner@clinic.in \
  --public-base-url https://billing.yourdomain.com

# 2. Paste the printed config into their sheet: Extensions → Apps Script
# 3. Create the Invoices_Ledger tab (16 columns, see DATA_MODEL.md)
# 4. Add one real invoice row, set Status = READY_TO_BILL
# 5. Run the SheetFlow menu → Process All READY_TO_BILL Rows
```

Then verify, in front of them:
1. Column I shows the correct GST total.
2. Column M is a `upi://` link — tap it on your phone, GPay opens pre-filled.
3. Column N opens a PDF with their GSTIN and logo-free clean layout.
4. Send one reminder: menu → **Trigger Due & Overdue Reminders**.

If all four work, ask for the setup fee before you leave.

## 6. What NOT to promise

- **WhatsApp reminders** — not built. Say "email and the sheet", never WhatsApp.
- **Automatic payment matching from their bank** — needs their gateway; only claim it if
  Razorpay keys are configured and the webhook is live.
- **Tally export / accounting entries** — not built.
- **"Any spreadsheet work"** — the automation does invoicing. Say that plainly.

## 7. First-week plan

1. Write down 10 local businesses with a paper/Excel invoice book.
2. Show 3 of them the PDF on your phone (it takes 20 seconds).
3. Onboard the first one free-ish, charge ₹2,000 setup, collect it.
4. Ask that client for two names. Referrals close 5× faster than cold visits.
5. Only then add a second client — one at a time, with `--create-tenant`.
