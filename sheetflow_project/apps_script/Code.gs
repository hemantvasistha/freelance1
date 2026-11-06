/**
 * Block 3: SheetFlow Google Apps Script Frontend Bridge (Code.gs)
 * Attach this script to the client's Google Sheet via Extensions -> Apps Script.
 * Matches the 16-column contract defined in DATA_MODEL.md.
 *
 * Credentials in this file are scoped to ONE tenant:
 *   - WEBHOOK_SECRET signs sheet-row webhooks (HMAC-SHA256 of the exact body).
 *   - OPERATOR_TOKEN identifies this tenant to the operator endpoints (reminders,
 *     digest, payment-status sync). It can only reach this tenant's records.
 * The server's global admin token must never be pasted here.
 */

const SHEETFLOW_CONFIG = {
  API_BASE_URL: "https://your-sheetflow-domain.example.com",
  TENANT_ID: "tnt_your_tenant_id",
  WEBHOOK_SECRET: "sec_live_000000000",
  OPERATOR_TOKEN: "op_paste-this-tenants-own-token",
  LEDGER_TAB_NAME: "Invoices_Ledger"
};

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu("⚡ SheetFlow Billing")
    .addItem("1. Process All READY_TO_BILL Rows", "processReadyRows")
    .addItem("2. Trigger Due & Overdue Reminders", "triggerDunningRun")
    .addSeparator()
    .addItem("3. Send Weekly Cash-Flow Digest", "requestWeeklyDigest")
    .addItem("4. Sync Payment Status from Server", "syncPaymentStatuses")
    .addToUi();
}

function processReadyRows() {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEETFLOW_CONFIG.LEDGER_TAB_NAME);
  if (!sheet) {
    SpreadsheetApp.getUi().alert("Tab '" + SHEETFLOW_CONFIG.LEDGER_TAB_NAME + "' not found!");
    return;
  }

  const data = sheet.getDataRange().getValues();
  let processedCount = 0;

  // Row 1 is headers (index 0); iterate from Row 2 (index 1)
  for (let i = 1; i < data.length; i++) {
    const row = data[i];
    const rowNumber = i + 1;
    const status = String(row[11] || "").trim(); // Col L: Status

    if (status !== "READY_TO_BILL") {
      continue;
    }

    const payload = {
      tenant_id: SHEETFLOW_CONFIG.TENANT_ID,
      sheet_row_number: rowNumber,
      invoice_number: String(row[0]).trim(),       // Col A
      client_name: String(row[1]).trim(),          // Col B
      client_email: String(row[2]).trim(),         // Col C
      client_phone: String(row[3] || "").trim(),   // Col D
      client_state: String(row[4] || "").trim(),   // Col E
      item_description: String(row[5]).trim(),     // Col F
      subtotal_major: String(row[6]).trim(),       // Col G
      tax_rate_pct: String(row[7] || "18.00").trim(), // Col H
      issue_date: formatDateIso(row[9]),           // Col J
      due_date: formatDateIso(row[10]),            // Col K
      status: "READY_TO_BILL"
    };

    // Sign the exact bytes we send; the backend verifies this HMAC before billing.
    const body = JSON.stringify(payload);
    const options = {
      method: "post",
      contentType: "application/json",
      headers: {
        "X-SheetFlow-Signature": hexHmac(SHEETFLOW_CONFIG.WEBHOOK_SECRET, body)
      },
      payload: body,
      muteHttpExceptions: true
    };

    const response = fetchWithRetry(
      SHEETFLOW_CONFIG.API_BASE_URL + "/api/v1/webhooks/sheet-row-ready",
      options
    );

    if (response.getResponseCode() === 200) {
      const result = JSON.parse(response.getContentText());
      sheet.getRange(rowNumber, 9).setValue(result.total_major);     // Col I: Verified Total
      sheet.getRange(rowNumber, 12).setValue(result.status);         // Col L: Status -> SENT
      sheet.getRange(rowNumber, 13).setValue(result.payment_url);    // Col M: Payment_Link
      // Col N: a signed HTTPS link the owner (or their customer) can actually open.
      // Falls back to the server-side path only if SHEETFLOW_PUBLIC_BASE_URL is unset.
      sheet.getRange(rowNumber, 14).setValue(result.pdf_url || result.pdf_path);
      // Col P: whether the invoice *email* actually went out (SENT/DRY_RUN/FAILED/SKIPPED).
      sheet.getRange(rowNumber, 16).setValue(result.delivery_status || "");
      processedCount++;
    } else {
      Logger.log("Row " + rowNumber + " error: " + response.getContentText());
    }
  }

  SpreadsheetApp.getUi().alert("SheetFlow processed " + processedCount + " invoice(s).");
}

/** Runs the T-3 / T+0 / T+7 dunning pass for this tenant as of today. */
function triggerDunningRun() {
  const today = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), "yyyy-MM-dd");
  const response = callOperatorApi("/api/v1/operator/dunning/run", "post", {
    as_of_date: today
  });

  if (response.getResponseCode() !== 200) {
    SpreadsheetApp.getUi().alert("Dunning run failed:\n" + response.getContentText());
    return;
  }

  const result = JSON.parse(response.getContentText());
  let message = "SheetFlow queued " + result.dispatched_count + " reminder(s) for " + today + ".";
  result.reminders.forEach(function (r) {
    message += "\n\n[" + r.stage + "] " + r.invoice_number + " — " + r.client_email +
      "\n" + r.subject + "\nDelivery: " + r.delivery_status;
  });
  SpreadsheetApp.getUi().alert(message);
}

/** Fetches and displays the weekly cash-flow digest for this tenant. */
function requestWeeklyDigest() {
  const today = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), "yyyy-MM-dd");
  const response = callOperatorApi(
    "/api/v1/operator/digest?as_of_date=" + today,
    "get",
    null
  );

  if (response.getResponseCode() !== 200) {
    SpreadsheetApp.getUi().alert("Digest request failed:\n" + response.getContentText());
    return;
  }

  const digest = JSON.parse(response.getContentText());
  SpreadsheetApp.getUi().alert(digest.formatted_summary);
}

/**
 * Pulls payment/overdue changes the server recorded and writes them into the sheet.
 *
 * Rows are matched on Invoice_No (column A), never on row position: the owner inserts,
 * deletes and sorts rows, and a positional write would corrupt the wrong invoice.
 * Only after every matched row is written is the batch acknowledged, so a failure
 * leaves the changes queued for the next run instead of losing them.
 */
function syncPaymentStatuses() {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEETFLOW_CONFIG.LEDGER_TAB_NAME);
  if (!sheet) {
    SpreadsheetApp.getUi().alert("Tab '" + SHEETFLOW_CONFIG.LEDGER_TAB_NAME + "' not found!");
    return;
  }

  const response = callOperatorApi("/api/v1/operator/sheet-updates", "get", null);
  if (response.getResponseCode() !== 200) {
    SpreadsheetApp.getUi().alert("Sync failed:\n" + response.getContentText());
    return;
  }

  const updates = JSON.parse(response.getContentText()).updates || [];
  if (!updates.length) {
    SpreadsheetApp.getUi().alert("Nothing to sync — the ledger is up to date.");
    return;
  }

  const data = sheet.getDataRange().getValues();
  const rowByInvoice = {};
  for (let i = 1; i < data.length; i++) {
    const key = String(data[i][0] || "").trim();
    if (key) {
      rowByInvoice[key] = i + 1; // 1-based sheet row
    }
  }

  const acknowledged = [];
  const missing = [];

  updates.forEach(function (u) {
    const rowNumber = rowByInvoice[String(u.invoice_number).trim()];
    if (!rowNumber) {
      missing.push(u.invoice_number);
      return;
    }
    sheet.getRange(rowNumber, 12).setValue(u.status);              // Col L: Status
    if (u.paid_at) {
      sheet.getRange(rowNumber, 15).setValue(u.paid_at);           // Col O: Paid_At
    }
    if (u.payment_ref) {
      sheet.getRange(rowNumber, 13).setValue(u.payment_ref);       // Col M: Payment ref
    }
    acknowledged.push(u.id);
  });

  if (acknowledged.length) {
    const ack = callOperatorApi("/api/v1/operator/sheet-updates/ack", "post", {
      ids: acknowledged
    });
    if (ack.getResponseCode() !== 200) {
      SpreadsheetApp.getUi().alert(
        "Wrote " + acknowledged.length + " row(s) but could not acknowledge them; " +
        "they will be re-sent next run.\n" + ack.getContentText()
      );
      return;
    }
  }

  SpreadsheetApp.getUi().alert(
    "Synced " + acknowledged.length + " payment update(s)." +
    (missing.length ? "\nNot found in this sheet: " + missing.join(", ") : "")
  );
}

/**
 * UrlFetchApp with bounded retries for transient failures (timeouts, 5xx, 429).
 * Invoice creation is idempotent server-side, so retrying a POST is safe.
 */
function fetchWithRetry(url, options, attempts) {
  const maxAttempts = attempts || 3;
  let lastError = null;

  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    try {
      const response = UrlFetchApp.fetch(url, options);
      const code = response.getResponseCode();
      const transient = code === 429 || code === 500 || code === 502 || code === 503 || code === 504;
      if (!transient) {
        return response;
      }
      lastError = new Error("HTTP " + code + ": " + response.getContentText());
    } catch (err) {
      // Network/DNS/timeout — retryable.
      lastError = err;
    }
    if (attempt < maxAttempts - 1) {
      Utilities.sleep(Math.pow(2, attempt) * 1000); // 1s, 2s, 4s
    }
  }

  Logger.log("Giving up on " + url + " after " + maxAttempts + " attempts: " + lastError);
  throw lastError;
}

/** Operator calls carry this tenant's own token — never the server admin token. */
function callOperatorApi(path, method, payload) {
  const options = {
    method: method,
    headers: { "X-SheetFlow-Operator-Token": SHEETFLOW_CONFIG.OPERATOR_TOKEN },
    muteHttpExceptions: true
  };
  if (payload) {
    options.contentType = "application/json";
    options.payload = JSON.stringify(payload);
  }
  return fetchWithRetry(SHEETFLOW_CONFIG.API_BASE_URL + path, options);
}

/** Hex HMAC-SHA256 of `message` keyed with `secret`. */
function hexHmac(secret, message) {
  const bytes = Utilities.computeHmacSha256Signature(message, secret);
  return bytes
    .map(function (b) {
      const v = (b < 0 ? b + 256 : b).toString(16);
      return v.length === 1 ? "0" + v : v;
    })
    .join("");
}

function formatDateIso(val) {
  if (val instanceof Date) {
    return Utilities.formatDate(val, Session.getScriptTimeZone(), "yyyy-MM-dd");
  }
  return String(val).trim();
}
