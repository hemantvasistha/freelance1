"""Client-openable invoice PDF links.

The sheet used to receive a server-local path (e.g. `generated_invoices\\INV-1.pdf`),
which is useless to the business owner and to their customer. Instead the API serves
the PDF over HTTP and the link carries an HMAC token derived from the tenant's
`webhook_secret`, so a link cannot be guessed or harvested across tenants.

Tokens have no expiry: an invoice link is meant to keep working for the customer's
records. Revoking one means rotating the tenant's `webhook_secret`.
"""
import hmac
from hashlib import sha256
from urllib.parse import quote


def compute_invoice_pdf_token(tenant_secret: str, tenant_id: str, invoice_number: str) -> str:
    message = f"invoice-pdf:{tenant_id}:{invoice_number}".encode("utf-8")
    return hmac.new(tenant_secret.encode("utf-8"), message, sha256).hexdigest()[:40]


def verify_invoice_pdf_token(
    tenant_secret: str, tenant_id: str, invoice_number: str, provided_token: str
) -> bool:
    expected = compute_invoice_pdf_token(tenant_secret, tenant_id, invoice_number)
    return hmac.compare_digest(expected, provided_token)


def build_invoice_pdf_url(
    public_base_url: str, tenant_secret: str, tenant_id: str, invoice_number: str
) -> str:
    """Absolute URL for `/api/v1/invoices/...`, or '' when no public base URL is set."""
    base = (public_base_url or "").strip().rstrip("/")
    if not base:
        return ""
    token = compute_invoice_pdf_token(tenant_secret, tenant_id, invoice_number)
    return (
        f"{base}/api/v1/invoices/{quote(tenant_id, safe='')}/"
        f"{quote(invoice_number, safe='')}/pdf?token={token}"
    )
