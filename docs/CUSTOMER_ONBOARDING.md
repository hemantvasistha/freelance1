# Onboarding a customer (≈45 minutes, once per client)

Assumes the server is already deployed with HTTPS (`deploy/README.md`).

## 0. Before you start

- `SHEETFLOW_PUBLIC_BASE_URL` on the server must be the real HTTPS origin. Without it,
  invoice PDF links are useless.
- Either configure SMTP and `DRY_RUN=false`, or keep `DRY_RUN=true` for the pilot and
  forward the invoice yourself. If `DRY_RUN=true`, **tell the customer** that emails are
  recorded, not delivered.
- Have the customer's Google Sheet open in a browser where you can edit it.

## 1. Create the tenant

```bash
cd /srv/sheetflow
sudo -u sheetflow .venv/bin/python sheetflow_engine.py --create-tenant \
  --tenant-id tnt_sunrise01 \
  --business-name "Sunrise Dental" \
  --home-state Maharashtra \
  --tax-id 27AABCS1429B1Z1 \
  --sheet-id <the sheet id from its URL> \
  --upi-vpa clinic@okaxis \
  --owner-email owner@clinic.in \
  --public-base-url https://billing.yourdomain.com
```

The JSON output contains `operator_token` and `apps_script_config`.

- `operator_token` is shown **once** and stored only as a hash. Paste it into the
  customer's script now; if you lose it, re-run onboarding to rotate.
- Never put `SHEETFLOW_ADMIN_TOKEN` in a customer's sheet. That token spans every
  tenant and belongs to your own tooling only.

## 2. Create the ledger tab

New tab named exactly `Invoices_Ledger`, header row (A→P):

`Invoice_No`, `Client_Name`, `Client_Email`, `Client_Phone`, `Client_State`,
`Item_Description`, `Subtotal`, `Tax_Rate_Pct`, `Total_Amount`, `Issue_Date`,
`Due_Date`, `Status`, `Payment_Link`, `PDF_Drive_URL`, `Paid_At`, `Last_Reminder`

Columns I, L, M, N, O and P are written by SheetFlow (or by the Apps Script on the
server's instruction). Leave them empty on new rows.

## 3. Attach the script

1. In the sheet: **Extensions → Apps Script**.
2. Paste `apps_script/Code.gs` as the only file.
3. Replace the `SHEETFLOW_CONFIG` block with the `apps_script_config` value from step 1.
   It sets `API_BASE_URL`, `TENANT_ID`, `WEBHOOK_SECRET`, `OPERATOR_TOKEN`,
   `LEDGER_TAB_NAME`.
4. Save, reload the sheet, and confirm the **⚡ SheetFlow Billing** menu appears.

## 4. Install automatic triggers (once, from the script editor)

Run the `installTriggers` function once (menu shows it as item 5). It creates:

- `syncPaymentStatuses` every 6 hours — pulls PAID/OVERDUE changes into the sheet.
- `runDailyMaintenance` daily at 08:00 — sync plus the reminder pass.

Without these, the sheet only syncs when somebody opens the menu, which is how ledgers
drift.

## 5. First invoice, in front of the customer

Add one row with real values, set `Status = READY_TO_BILL`, then
**SheetFlow menu → 1. Process All READY_TO_BILL Rows**.

Verify all five, and say what you are checking:

1. Column I shows the GST-correct total (compare with the customer's own arithmetic).
2. Column M is a `upi://` link (or a Razorpay link). Tap it on a phone: GPay/PhonePe
   opens pre-filled with the amount.
3. Column N opens the PDF, with their GSTIN and address.
4. Column P shows the email delivery state: `SENT`, `DRY_RUN`, `FAILED` or `SKIPPED`.
   Explain which one they will see.
5. Column L changed from `READY_TO_BILL` to `SENT`.

Then ask for the setup fee before leaving.

## 6. What to promise, and what not to

Promise: PDF invoices, a payment link that works on a phone, and reminders that send
themselves, with a Monday summary.

Do **not** promise WhatsApp, automatic bank matching, Tally export, or "full GST
compliance". The tax engine handles one rate per invoice and intra/inter-state split.

## 7. Rotation and offboarding

- Lost or leaked token: re-run step 1 with the same `--tenant-id`; the new
  `operator_token` replaces the old one immediately. Update the script.
- Offboarding: stop using the tenant, export the sheet, then delete the tenant row and
  the generated PDFs. There is no self-service account deletion yet; do it in SQL and
  record it.
