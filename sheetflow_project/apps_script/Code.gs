/**
 * Block 3: SheetFlow Google Apps Script Frontend Bridge (Code.gs)
 * Attach this script to the client's Google Sheet via Extensions -> Apps Script.
 * Matches the 16-column contract defined in DATA_MODEL.md.
 *
 * Every request to the backend is authenticated:
 *   - sheet-row webhooks carry X-SheetFlow-Signature (hex HMAC-SHA256 of the raw body,
 *     keyed with WEBHOOK_SECRET), so nobody can bill a tenant without the shared secret.
 *   - operator endpoints carry X-SheetFlow-Admin-Token (matches SHEETFLOW_ADMIN_TOKEN
 *     on the server).
 */

const SHEETFLOW_CONFIG = {
  API_BASE_URL: "https://your-sheetflow-domain.example.com",
  TENANT_ID: "tnt_bahal_01",
  WEBHOOK_SECRET: "sec_live_987654321",
  ADMIN_TOKEN: "change-me-admin-token",
  LEDGER_TAB_NAME: "Invoices_Ledger"
};

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu("⚡ SheetFlow Billing")
    .addItem("1. Process All READY_TO_BILL Rows", "processReadyRows")
    .addItem("2. Trigger Due & Overdue Reminders", "triggerDunningRun")
    .addSeparator()
    .addItem("3. Send Weekly Cash-Flow Digest", "requestWeeklyDigest")
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
  const response = callOperatorApi("/api/v1/dunning/run", "post", {
    tenant_id: SHEETFLOW_CONFIG.TENANT_ID,
    as_of_date: today
  });

  if (response.getResponseCode() !== 200) {
    SpreadsheetApp.getUi().alert("Dunning run failed:\n" + response.getContentText());
    return;
  }

  const result = JSON.parse(response.getContentText());
  let message = "SheetFlow queued " + result.dispatched_count + " reminder(s) for " + today + ".";
  result.reminders.forEach(function (r) {
    message += "\n\n[" + r.stage + "] " + r.invoice_number + " — " + r.client_email + "\n" + r.subject;
  });
  SpreadsheetApp.getUi().alert(message);
}

/** Fetches and displays the weekly cash-flow digest for this tenant. */
function requestWeeklyDigest() {
  const today = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), "yyyy-MM-dd");
  const response = callOperatorApi(
    "/api/v1/digest/" + encodeURIComponent(SHEETFLOW_CONFIG.TENANT_ID) + "?as_of_date=" + today,
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

function callOperatorApi(path, method, payload) {
  const options = {
    method: method,
    headers: { "X-SheetFlow-Admin-Token": SHEETFLOW_CONFIG.ADMIN_TOKEN },
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
