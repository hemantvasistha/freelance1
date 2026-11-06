"""Block 2: HTML & A4 PDF Invoice Renderer (Archetype 1 Flat Booktabs Design).

The HTML template is always written to disk. PDF rendering uses WeasyPrint when its
native dependencies (pango/cairo) are importable, and otherwise falls back to a
ReportLab renderer so invoices still generate on machines without GTK installed
(notably a stock Windows box).
"""
import html
import logging
import os
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache

from src.models import InvoiceRecord, TenantRecord
from src.schemas import format_major_currency


def render_invoice_html(
    tenant: TenantRecord,
    invoice: InvoiceRecord,
    payment_url: str,
    upi_uri: str | None = None,
) -> str:
    """Renders an A4 print-ready HTML invoice adhering to clean flat-canvas typography."""
    symbol = "₹" if tenant.currency_code.upper() == "INR" else f"{tenant.currency_code} "
    subtotal_str = format_major_currency(invoice.subtotal_minor, symbol)
    cgst_str = format_major_currency(invoice.cgst_minor, symbol)
    sgst_str = format_major_currency(invoice.sgst_minor, symbol)
    igst_str = format_major_currency(invoice.igst_minor, symbol)
    total_str = format_major_currency(invoice.total_minor, symbol)

    tax_rate_pct = (Decimal(invoice.tax_rate_bps) / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    half_rate_pct = (tax_rate_pct / Decimal("2")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    if invoice.cgst_minor > 0 or invoice.sgst_minor > 0:
        tax_rows_html = f"""
        <tr>
          <td>CGST ({half_rate_pct}%) — Intra-State ({html.escape(tenant.home_state)})</td>
          <td class="num">{cgst_str}</td>
        </tr>
        <tr>
          <td>SGST ({half_rate_pct}%) — Intra-State ({html.escape(tenant.home_state)})</td>
          <td class="num">{sgst_str}</td>
        </tr>
        """
    elif invoice.igst_minor > 0:
        tax_rows_html = f"""
        <tr>
          <td>IGST ({tax_rate_pct}%) — Inter-State ({html.escape(tenant.home_state)} to {html.escape(invoice.client_state or 'Out-of-State')})</td>
          <td class="num">{igst_str}</td>
        </tr>
        """
    else:
        tax_rows_html = """
        <tr>
          <td>Tax (0.00% — Exempt / Zero-Rated)</td>
          <td class="num">₹0.00</td>
        </tr>
        """

    upi_line = (
        f"<p><strong>UPI Instant Pay VPA:</strong> {html.escape(tenant.upi_vpa)} "
        f"(Reference: {html.escape(invoice.invoice_number)})</p>"
        if tenant.upi_vpa
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Tax Invoice {html.escape(invoice.invoice_number)}</title>
  <style>
    :root {{
      --primary: #1e3a5f;
      --accent: #0f766e;
      --text: #1f2937;
      --bg: #ffffff;
    }}
    @page {{
      size: A4;
      margin: 22mm 20mm;
      background: var(--bg);
      @bottom-right {{
        content: counter(page);
        font-family: 'Inter', 'Noto Sans', sans-serif;
        font-size: 8.5pt;
        color: var(--text);
      }}
    }}
    *, *::before, *::after {{ box-sizing: border-box; }}
    html, body {{
      margin: 0;
      padding: 0;
      background: transparent;
    }}
    body {{
      font-family: 'Inter', 'Noto Sans', sans-serif;
      font-size: 10.5pt;
      line-height: 1.6;
      color: var(--text);
    }}
    @media screen {{
      html, body {{ background: var(--bg); }}
      body {{ max-width: 900px; margin: 0 auto; padding: 32px 40px; }}
    }}
    main.document-flow {{ width: 100%; margin: 0; padding: 0; }}
    * {{ border: none; border-radius: 0; box-shadow: none; background: transparent; outline: none; }}
    h1 {{ font-family: 'Inter', 'Noto Sans', sans-serif; font-size: 20pt; line-height: 1.25; margin: 0 0 8px 0; color: var(--primary); }}
    h2 {{ font-family: 'Inter', 'Noto Sans', sans-serif; font-size: 13pt; line-height: 1.35; margin: 32px 0 12px 0; color: var(--primary); break-after: avoid; page-break-after: avoid; }}
    h3 {{ font-size: 11pt; line-height: 1.35; margin: 20px 0 8px 0; color: var(--accent); }}
    p {{ margin: 0 0 12px 0; }}
    section {{ margin-bottom: 24px; }}
    table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
    th, td {{ text-align: left; padding: 10px 12px; font-size: 9.5pt; vertical-align: top; }}
    th {{ border-bottom: 2px solid var(--primary); font-weight: 600; color: var(--primary); }}
    td {{ border-bottom: 1px solid rgba(0,0,0,0.08); }}
    th.num, td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    hr {{ border-bottom: 1px solid rgba(0,0,0,0.15); margin: 22px 0; }}
    .full-bleed-header {{ background: var(--primary); color: #ffffff; padding: 24px 28px; margin-bottom: 24px; }}
    .full-bleed-header h1 {{ color: #ffffff; margin: 0 0 6px 0; }}
    .full-bleed-header p {{ color: #e2e8f0; margin: 0; font-size: 9.5pt; }}
    a {{ color: var(--accent); text-decoration: underline; }}
    tr, figure {{ break-inside: avoid; page-break-inside: avoid; }}
  </style>
</head>
<body>
  <main class="document-flow">
    <div class="full-bleed-header">
      <h1>TAX INVOICE — {html.escape(invoice.invoice_number)}</h1>
      <p>Issued by {html.escape(tenant.business_name)} | GSTIN/Tax ID: {html.escape(tenant.tax_id or 'Unregistered')} | State: {html.escape(tenant.home_state)}</p>
    </div>

    <section>
      <h2>1. Billing Parties &amp; Schedule Metadata</h2>
      <table>
        <thead>
          <tr>
            <th>Supplier (From)</th>
            <th>Billed Client (To)</th>
            <th>Invoice Timeline &amp; Status</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>
              <strong>{html.escape(tenant.business_name)}</strong><br>
              State: {html.escape(tenant.home_state)}<br>
              Tax ID: {html.escape(tenant.tax_id or 'N/A')}
            </td>
            <td>
              <strong>{html.escape(invoice.client_name)}</strong><br>
              Email: {html.escape(invoice.client_email)}<br>
              Phone: {html.escape(invoice.client_phone or 'N/A')}<br>
              Place of Supply: {html.escape(invoice.client_state or tenant.home_state)}
            </td>
            <td>
              <strong>Invoice No:</strong> {html.escape(invoice.invoice_number)}<br>
              <strong>Issue Date:</strong> {html.escape(invoice.issue_date)}<br>
              <strong>Due Date:</strong> {html.escape(invoice.due_date)}<br>
              <strong>Status:</strong> {html.escape(invoice.status)}
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <section>
      <h2>2. Itemized Scope &amp; Deterministic Tax Breakdown</h2>
      <table>
        <thead>
          <tr>
            <th>Description of Services / Goods</th>
            <th class="num">Amount ({html.escape(tenant.currency_code)})</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td><strong>{html.escape(invoice.item_description)}</strong><br>Spreadsheet Row Reference: #{invoice.sheet_row_number}</td>
            <td class="num">{subtotal_str}</td>
          </tr>
          {tax_rows_html}
          <tr>
            <td><strong>Total Amount Payable (Inclusive of GST/Taxes)</strong></td>
            <td class="num"><strong>{total_str}</strong></td>
          </tr>
        </tbody>
      </table>
    </section>

    <hr>

    <section>
      <h2>3. Instant Payment &amp; Automated Reconciliation Instructions</h2>
      <p>To ensure automatic ledger reconciliation and halt scheduled reminder alerts, complete payment on or before <strong>{html.escape(invoice.due_date)}</strong> using one of the options below.</p>
      {upi_line}
      {f'<p><strong>Pay link:</strong> <a href="{html.escape(payment_url)}">{html.escape(payment_url)}</a></p>' if (payment_url and not tenant.upi_vpa) else ''}
      <p><strong>Idempotency Verification Digest:</strong> {html.escape(invoice.idempotency_key[:24])}...</p>
    </section>
  </main>
</body>
</html>
"""


logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _weasyprint_available() -> bool:
    """True when WeasyPrint and its native pango/cairo libraries can be imported."""
    try:
        import weasyprint  # noqa: F401
    except Exception as exc:  # OSError when libgobject/pango is missing (Windows)
        logger.info("WeasyPrint unavailable (%s); falling back to ReportLab.", exc)
        return False
    return True


def active_pdf_engine() -> str:
    """Name of the PDF backend this process will use: 'weasyprint' or 'reportlab'."""
    return "weasyprint" if _weasyprint_available() else "reportlab"


@lru_cache(maxsize=1)
def _pdf_font() -> str:
    """Registers a Unicode-capable font so the rupee sign renders; falls back to Helvetica."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [
        os.environ.get("SHEETFLOW_PDF_FONT", ""),
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        try:
            pdfmetrics.registerFont(TTFont("SheetFlowSans", path))
            return "SheetFlowSans"
        except Exception:
            continue
    return "Helvetica"


def _render_pdf_reportlab(
    tenant: TenantRecord,
    invoice: InvoiceRecord,
    payment_url: str,
    upi_uri: str | None,
    pdf_path: str,
) -> None:
    """Fallback A4 renderer used when WeasyPrint's native libraries are unavailable."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    font = _pdf_font()
    symbol = "₹" if tenant.currency_code.upper() == "INR" else f"{tenant.currency_code} "

    def text(value: object) -> str:
        out = str(value)
        # Standard PDF fonts are Latin-1 only, so spell out the rupee sign there.
        return out.replace("₹", "Rs ") if font == "Helvetica" else out

    width, height = A4
    left, right = 56.0, width - 56.0
    c = canvas.Canvas(pdf_path, pagesize=A4)
    y = height - 56.0

    c.setFillColorRGB(0.118, 0.227, 0.373)
    c.rect(left, y - 58, right - left, 58, stroke=0, fill=1)
    c.setFillColorRGB(1, 1, 1)
    c.setFont(font, 16)
    c.drawString(left + 16, y - 26, text(f"TAX INVOICE — {invoice.invoice_number}"))
    c.setFont(font, 8.5)
    c.drawString(
        left + 16,
        y - 44,
        text(
            f"{tenant.business_name} | Tax ID: {tenant.tax_id or 'Unregistered'} | "
            f"State: {tenant.home_state}"
        ),
    )
    y -= 58 + 26

    c.setFillColorRGB(0.12, 0.23, 0.37)
    c.setFont(font, 11)
    c.drawString(left, y, text("1. Billing Parties & Schedule Metadata"))
    y -= 16
    c.setFillColorRGB(0.12, 0.12, 0.16)
    c.setFont(font, 9)
    for line in (
        f"From: {tenant.business_name} ({tenant.home_state})",
        f"To: {invoice.client_name} <{invoice.client_email}>",
        f"Place of Supply: {invoice.client_state or tenant.home_state}",
        f"Issue Date: {invoice.issue_date}    Due Date: {invoice.due_date}    Status: {invoice.status}",
    ):
        c.drawString(left, y, text(line))
        y -= 13

    y -= 12
    c.setFont(font, 11)
    c.drawString(left, y, text("2. Itemized Scope & Deterministic Tax Breakdown"))
    y -= 18

    def money_line(label: str, amount: str, bold: bool = False) -> None:
        nonlocal y
        c.setFont(font, 9.5 if bold else 9)
        c.drawString(left, y, text(label))
        c.drawRightString(right, y, text(amount))
        y -= 15

    c.setFont(font, 9)
    c.drawString(left, y, text("Description of Services / Goods"))
    c.drawRightString(right, y, text(f"Amount ({tenant.currency_code})"))
    y -= 6
    c.setStrokeColorRGB(0.12, 0.23, 0.37)
    c.setLineWidth(1.2)
    c.line(left, y, right, y)
    y -= 15
    money_line(f"{invoice.item_description} (row #{invoice.sheet_row_number})", format_major_currency(invoice.subtotal_minor, symbol))
    if invoice.cgst_minor or invoice.sgst_minor:
        half = (Decimal(invoice.tax_rate_bps) / Decimal("200")).quantize(Decimal("0.01"))
        money_line(f"CGST ({half}%) — Intra-State ({tenant.home_state})", format_major_currency(invoice.cgst_minor, symbol))
        money_line(f"SGST ({half}%) — Intra-State ({tenant.home_state})", format_major_currency(invoice.sgst_minor, symbol))
    elif invoice.igst_minor:
        rate = (Decimal(invoice.tax_rate_bps) / Decimal("100")).quantize(Decimal("0.01"))
        money_line(f"IGST ({rate}%) — Inter-State", format_major_currency(invoice.igst_minor, symbol))
    else:
        money_line("Tax (0% — Exempt / Zero-Rated)", format_major_currency(0, symbol))
    y -= 2
    c.line(left, y, right, y)
    y -= 16
    money_line("Total Amount Payable (Inclusive of GST/Taxes)", format_major_currency(invoice.total_minor, symbol), bold=True)

    y -= 16
    c.setFont(font, 11)
    c.drawString(left, y, text("3. Payment & Reconciliation"))
    y -= 16
    c.setFont(font, 9)
    c.drawString(left, y, text(f"Pay on or before {invoice.due_date} using one of the options below:"))
    y -= 13
    if payment_url and not tenant.upi_vpa:
        c.setFillColorRGB(0.059, 0.463, 0.431)
        c.drawString(left, y, text(payment_url[:110]))
        y -= 13
    if upi_uri:
        c.setFillColorRGB(0.12, 0.12, 0.16)
        c.drawString(left, y, text(f"UPI: {tenant.upi_vpa} (ref {invoice.invoice_number})"))
        y -= 13
    c.setFillColorRGB(0.45, 0.45, 0.5)
    c.setFont(font, 7.5)
    c.drawString(left, y, text(f"Idempotency digest: {invoice.idempotency_key[:24]}..."))
    c.showPage()
    c.save()


def generate_invoice_pdf(
    tenant: TenantRecord,
    invoice: InvoiceRecord,
    payment_url: str,
    output_dir: str,
    upi_uri: str | None = None,
) -> tuple[str, str]:
    """
    Writes both the `.html` source and `.pdf` deliverable for the given invoice.
    Returns `(html_path, pdf_path)`.
    """
    os.makedirs(output_dir, exist_ok=True)
    safe_inv = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in invoice.invoice_number)
    html_path = os.path.join(output_dir, f"{safe_inv}.html")
    pdf_path = os.path.join(output_dir, f"{safe_inv}.pdf")

    html_content = render_invoice_html(tenant, invoice, payment_url, upi_uri=upi_uri)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    if _weasyprint_available():
        import weasyprint

        weasyprint.HTML(string=html_content).write_pdf(pdf_path)
    else:
        _render_pdf_reportlab(tenant, invoice, payment_url, upi_uri, pdf_path)
    return html_path, pdf_path
